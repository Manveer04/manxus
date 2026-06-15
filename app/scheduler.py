import os
import time
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from app.database import SessionLocal
from app.sync_engine import SyncEngine
from app.order_engine import OrderEngine
try:
    from app.scrapers.shopee_api import _refresh_token, _load_token, SHOPS
except Exception:
    SHOPS = {}

    def _load_token(shop_id):
        return None

    def _refresh_token(shop_id):
        return None

scheduler = AsyncIOScheduler()
_engine       = SyncEngine()
_order_engine = OrderEngine()

INTERVAL       = int(os.getenv("SYNC_INTERVAL_MINUTES", "30"))
ORDER_INTERVAL = int(os.getenv("ORDER_INTERVAL_MINUTES", "10"))
NOTIFY_RETRY_INTERVAL = int(os.getenv("NOTIFY_RETRY_INTERVAL_MINUTES", "5"))
AUTO_SYNC      = os.getenv("AUTO_SYNC_ENABLED", "true").lower() == "true"
ORDER_SYNC_ON_STARTUP = os.getenv("ORDER_SYNC_ON_STARTUP", "true").lower() == "true"
ORDER_SYNC_STARTUP_DELAY_SECONDS = int(os.getenv("ORDER_SYNC_STARTUP_DELAY_SECONDS", "20"))

async def _auto_pull():
    db = SessionLocal()
    started_at = time.time()
    try:
        print("[Scheduler][auto_pull] START")
        results = await _engine.pull_all(db)
        total_pulled = sum((v or {}).get("pulled", 0) for v in (results or {}).values())
        total_errors = sum((v or {}).get("errors", 0) for v in (results or {}).values())
        print(
            f"[Scheduler][auto_pull] DONE pulled={total_pulled} "
            f"errors={total_errors} platforms={len(results or {})} "
            f"elapsed={time.time() - started_at:.2f}s"
        )
    except Exception as e:
        print(f"[Scheduler][auto_pull] ERROR err={e}")
    finally:
        db.close()

async def _auto_push_out_of_sync():
    db = SessionLocal()
    started_at = time.time()
    try:
        print("[Scheduler][auto_push_out_of_sync] START")
        result = await _engine.push_all_out_of_sync(db)
        synced = result.get("synced", []) if isinstance(result, dict) else []
        print(
            f"[Scheduler][auto_push_out_of_sync] DONE products={len(synced)} "
            f"elapsed={time.time() - started_at:.2f}s"
        )
    except Exception as e:
        print(f"[Scheduler][auto_push_out_of_sync] ERROR err={e}")
    finally:
        db.close()

async def _auto_fetch_orders():
    db = SessionLocal()
    started_at = time.time()
    try:
        print("[Scheduler][auto_fetch_orders] START")
        results = await _order_engine.fetch_all_orders(db)
        print(
            "[Scheduler][auto_fetch_orders] DONE "
            f"results={results} elapsed={time.time() - started_at:.2f}s"
        )
    except Exception as e:
        # Keep scheduler healthy even if one run fails.
        print(f"[Scheduler][auto_fetch_orders] ERROR err={e}")
    finally:
        db.close()

async def _auto_retry_notifications():
    db = SessionLocal()
    started_at = time.time()
    try:
        print("[Scheduler][auto_retry_notifications] START")
        retried = await _order_engine.retry_missed_notifications(db)
        print(
            f"[Scheduler][auto_retry_notifications] DONE retried={retried} "
            f"elapsed={time.time() - started_at:.2f}s"
        )
    except Exception as e:
        print(f"[Scheduler][auto_retry_notifications] ERROR err={e}")
    finally:
        db.close()

def start_scheduler():
    if not AUTO_SYNC:
        return
    scheduler.add_job(
        _auto_pull,
        trigger=IntervalTrigger(minutes=INTERVAL),
        id="auto_pull",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
    scheduler.add_job(
        _auto_push_out_of_sync,
        trigger=IntervalTrigger(minutes=INTERVAL),
        id="auto_push",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )

    next_order_run = None
    if ORDER_SYNC_ON_STARTUP:
        next_order_run = datetime.now() + timedelta(seconds=max(0, ORDER_SYNC_STARTUP_DELAY_SECONDS))

    scheduler.add_job(
        _auto_fetch_orders,
        trigger=IntervalTrigger(minutes=ORDER_INTERVAL),
        id="auto_orders",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
        next_run_time=next_order_run,
    )
    scheduler.add_job(
        _auto_retry_notifications,
        trigger=IntervalTrigger(minutes=NOTIFY_RETRY_INTERVAL),
        id="auto_notify_retry",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
    scheduler.add_job(
        _refresh_shopee_tokens,
        trigger=IntervalTrigger(hours=3),
        id="shopee_token_refresh",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
    scheduler.start()
    print(
        f"[Scheduler] Inventory sync every {INTERVAL} min | "
        f"Order check every {ORDER_INTERVAL} min | "
        f"Notify retry every {NOTIFY_RETRY_INTERVAL} min | "
        f"Order startup run={'on' if ORDER_SYNC_ON_STARTUP else 'off'}"
    )

async def _refresh_shopee_tokens():
    import time
    for key, shop in SHOPS.items():
        shop_id = shop["shop_id"]
        if not shop_id:
            continue
        try:
            record = _load_token(shop_id)
            if not record:
                print(f"[Scheduler] No Shopee token for {shop['name']} — skipping")
                continue
            age     = int(time.time()) - record["fetched_at"]
            expires = record["expire_in"]
            if age >= expires - 3600:
                _refresh_token(shop_id)
                print(f"[Scheduler] Shopee token refreshed for {shop['name']}")
            else:
                print(f"[Scheduler] Shopee token OK for {shop['name']} ({(expires-age)//60}m left)")
        except Exception as e:
            print(f"[Scheduler] Shopee token refresh failed for {shop['name']}: {e}")


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown()