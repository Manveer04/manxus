"""
Order engine — fetches new orders from Lazada and TikTok,
stores them in DB, and sends ntfy notifications.
"""
import json
import os
import re
import time
from datetime import datetime, timedelta
from typing import List, Dict, Any
from urllib.parse import quote_plus

import httpx
from sqlalchemy.orm import Session

from app.models import Order, OrderItem, PlatformListing, Product
from app.notifier import notify_new_order
from app.marketplace import marketplace_unavailable


class OrderEngine:

    def __init__(self, scraper=None, shop_cipher: str | None = None):
        # Optional external scrapers / tokens may be provided by callers.
        # If not provided, marketplace-specific fetch methods will raise
        # marketplace_unavailable so callers can handle the lack of integration.
        self.scraper = scraper
        self.shop_cipher = shop_cipher


    def _build_order_action_url(self, order: Order) -> str:
        """Build a deep link to Orders Admin for fast order handling from notification."""
        base_url = os.getenv("ORDER_ACTION_BASE_URL", os.getenv("PUBLIC_BASE_URL", "http://192.168.50.129:8080")).rstrip("/")
        platform = quote_plus(str(order.platform or ""))
        platform_order_id = quote_plus(str(order.platform_order_id or ""))
        return f"{base_url}/orders-admin?platform={platform}&order={platform_order_id}"

    async def _notify_and_mark(self, db: Session, order: Order, items: List[Dict[str, Any]]) -> bool:
        """Send notification and persist delivery status on the order row."""
        action_url = self._build_order_action_url(order)
        ok = await notify_new_order(
            platform=order.platform,
            order_id=order.platform_order_id,
            buyer=order.buyer_name or "Unknown",
            total=float(order.total_price or 0),
            items=items,
            action_url=action_url,
        )
        order.notified = bool(ok)
        db.commit()
        if not ok:
            print(f"[OrderEngine] Notification failed for {order.platform} order {order.platform_order_id}; will retry later")
        return ok

    async def retry_missed_notifications(self, db: Session, limit: int = 200) -> int:
        """Retry notifications for orders that were stored but not successfully notified."""
        pending = (
            db.query(Order)
            .filter(Order.notified.is_(False))
            .order_by(Order.created_at.asc())
            .limit(limit)
            .all()
        )
        if not pending:
            return 0

        sent = 0
        for order in pending:
            items = [
                {
                    "name": self._resolve_display_name(db, order.platform, i.platform_sku, i.product_name),
                    "quantity": i.quantity,
                }
                for i in order.items
            ]
            ok = await self._notify_and_mark(db, order, items)
            if ok:
                sent += 1
        if sent:
            print(f"[OrderEngine] Retried and sent {sent} missed notification(s)")
        return sent

    async def notify_order_by_id(self, db: Session, order_id: int) -> bool:
        """Manually send or resend notification for a specific stored order."""
        order = db.query(Order).filter_by(id=order_id).first()
        if not order:
            return False

        items = [
            {
                "name": self._resolve_display_name(db, order.platform, i.platform_sku, i.product_name),
                "quantity": i.quantity,
            }
            for i in order.items
        ]
        return await self._notify_and_mark(db, order, items)

    def _resolve_display_name(self, db, platform: str, platform_sku: str, fallback_name: str) -> str:
        """Look up group display name or product name for a platform SKU."""
        try:
            listing = db.query(PlatformListing).filter_by(
                platform=platform, platform_sku=platform_sku
            ).first()
            if listing and listing.product:
                product = listing.product
                # Check if product belongs to a group
                if product.groups:
                    return product.groups[0].display_name
                return product.name
        except Exception:
            pass
        return fallback_name or "Unknown Product"

    @staticmethod
    def _to_float(value: Any, default: float = 0.0) -> float:
        try:
            if value is None:
                return default
            if isinstance(value, (int, float)):
                return float(value)
            s = str(value).strip()
            if not s:
                return default
            s = re.sub(r"[^0-9.\-]", "", s)
            return float(s) if s else default
        except Exception:
            return default

    def _money_from_shopee(self, value: Any) -> float:
        """Shopee often uses 1/100000 precision; this normalizes to RM."""
        raw = self._to_float(value, 0.0)
        if raw <= 0:
            return 0.0
        # Heuristic: large integers are micro-units; decimal values are already RM.
        return raw / 100000.0 if raw >= 1000 else raw

    def _extract_shopee_total(self, pkg: dict, items_raw: List[dict]) -> float:
        """Read total from several Shopee payload shapes, fallback to item sum."""
        payment = pkg.get("payment_info", {}) or {}
        for key in ("total_price", "final_total", "buyer_total", "total"):
            total = self._money_from_shopee(payment.get(key))
            if total > 0:
                return total

        footer = pkg.get("card_footer", {}) or {}
        for section in footer.get("price_section", []) or []:
            if section.get("is_total"):
                for key in ("price_value", "value", "amount"):
                    total = self._money_from_shopee(section.get(key))
                    if total > 0:
                        return total
                txt = section.get("display_text") or section.get("text")
                total = self._to_float(txt, 0.0)
                if total > 0:
                    return total

        calc = sum(float(i.get("price", 0.0) or 0.0) * int(i.get("quantity", 1) or 1) for i in items_raw)
        return round(calc, 2)

    # ─── Lazada ──────────────────────────────────────────────────────────────

    async def fetch_lazada_orders(self, db: Session, scraper=None):
        """Fetch recent Lazada orders and store new ones."""
        # Require a configured scraper for Lazada API access
        scraper = scraper or self.scraper
        if not scraper:
            raise marketplace_unavailable("Lazada order fetch", "lazada")

        # Fetch orders from last 2 hours to catch anything since last poll
        since = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S+08:00")
        statuses = ["pending", "ready_to_ship", "delivered", "returned", "canceled", "failed"]
        all_orders = []

        async with httpx.AsyncClient() as client:
            for status in statuses:
                offset = 0
                while True:
                    extra = {
                        "created_after": since,
                        "status": status,
                        "limit": "50",
                        "offset": str(offset),
                        "sort_by": "created_at",
                        "sort_direction": "DESC",
                    }
                    params = scraper._base_params("/orders/get", extra)
                    r = await client.get(
                        "https://api.lazada.com.my/rest/orders/get",
                        params=params,
                        timeout=30,
                    )
                    data = r.json()
                    if data.get("code") not in (None, "0", 0):
                        print(f"[OrderEngine] Lazada orders error ({status}): {data.get('message')}")
                        break
                    orders = data.get("data", {}).get("orders", [])
                    if not orders:
                        break
                    all_orders.extend(orders)
                    total = data.get("data", {}).get("countTotal", 0)
                    offset += 50
                    if offset >= total:
                        break

        new_count = 0
        for o in all_orders:
            order_id = str(o.get("order_number", ""))
            if not order_id:
                continue
            existing = db.query(Order).filter_by(platform="lazada", platform_order_id=order_id).first()
            if existing:
                # Update status if changed
                new_status = self._lazada_status(o.get("statuses", []))
                if existing.status != new_status:
                    existing.status = new_status
                    db.commit()
                continue

            # Parse created_at
            created_at = None
            try:
                created_at = datetime.strptime(o["created_at"], "%Y-%m-%d %H:%M:%S %z")
                created_at = created_at.replace(tzinfo=None)
            except Exception:
                pass

            status_str = self._lazada_status(o.get("statuses", []))
            buyer = (o.get("customer_first_name", "") + " " + o.get("customer_last_name", "")).strip()
            buyer = buyer or "Unknown"

            order = Order(
                platform="lazada",
                platform_order_id=order_id,
                status=status_str,
                buyer_name=buyer,
                total_price=float(o.get("price", 0) or 0),
                shipping_fee=float(o.get("shipping_fee", 0) or 0),
                payment_method=o.get("payment_method", ""),
                items_count=int(o.get("items_count", 0) or 0),
                notified=False,
                platform_created_at=created_at,
            )
            db.add(order)
            db.flush()

            # Fetch order items
            try:
                items_data = await self._lazada_order_items(scraper, order_id)
                for item in items_data:
                    db.add(OrderItem(
                        order_id=order.id,
                        platform_sku=item.get("sku"),
                        product_name=item.get("name"),
                        quantity=item.get("quantity", 1),
                        unit_price=item.get("price", 0.0),
                    ))
            except Exception as e:
                print(f"[OrderEngine] Failed to fetch Lazada items for {order_id}: {e}")

            db.commit()
            new_count += 1

            # Send ntfy notification
            await self._notify_and_mark(
                db,
                order,
                [{
                    "name": self._resolve_display_name(db, "lazada", i.platform_sku, i.product_name),
                    "quantity": i.quantity
                } for i in order.items],
            )

        print(f"[OrderEngine] Lazada: {new_count} new orders")
        return new_count

    async def _lazada_order_items(self, scraper, order_id: str) -> List[Dict]:
        extra  = {"order_id": order_id}
        params = scraper._base_params("/order/items/get", extra)
        async with httpx.AsyncClient() as client:
            r = await client.get(
                "https://api.lazada.com.my/rest/order/items/get",
                params=params,
                timeout=30,
            )
        data  = r.json()
        items = data.get("data", []) or []
        result = []
        for item in items:
            result.append({
                "sku":      item.get("sku", ""),
                "name":     item.get("name", ""),
                "quantity": 1,  # Lazada items are individual rows
                "price":    float(item.get("item_price", 0) or 0),
            })
        return result

    def _lazada_status(self, statuses: list) -> str:
        if not statuses:
            return "unknown"
        return statuses[0].lower().replace(" ", "_")

    # ─── TikTok ──────────────────────────────────────────────────────────────

    async def fetch_tiktok_orders(self, db: Session):
        """Fetch recent TikTok orders and store new ones."""
        # Require a configured scraper and shop cipher for TikTok API access
        scraper = getattr(self, "scraper", None)
        shop_cipher = getattr(self, "shop_cipher", None)
        if not scraper or not shop_cipher:
            raise marketplace_unavailable("TikTok order fetch", "tiktok")

        path = "/order/202309/orders/search"
        # page_size is a query param; create_time filters go in the body
        body = {
            "create_time_ge": int(time.time()) - 86400,
            "create_time_lt": int(time.time()),
        }
        body_str = json.dumps(body, separators=(",", ":"))
        params   = scraper._base_params(path, {"shop_cipher": shop_cipher, "page_size": 50}, body_str)

        async with httpx.AsyncClient() as client:
            r = await client.post(
                scraper.BASE_URL + path,
                params=params,
                headers=scraper._headers(),
                content=body_str,
                timeout=30,
            )
        data   = scraper._safe_json(r)
        if data.get("code") not in (None, 0, "0"):
            raise Exception(f"TikTok orders API error: code={data.get('code')} message={data.get('message')} request_id={data.get('request_id')}")
        orders = (data.get("data") or {}).get("orders", [])
        print(f"[OrderEngine] TikTok raw: code={data.get('code')}, order_count={len(orders)}")

        # Visibility: highlight the first truly shippable order/package seen.
        shippable_statuses = {
            "AWAITING_SHIPMENT",
            "AWAITING_COLLECTION",
            "TO_SHIP",
            "READY_TO_SHIP",
            "UNSHIPPED",
            "WAIT_SELLER_SEND_GOODS",
        }
        for o in orders:
            order_status = str(o.get("status") or "").upper()
            package_statuses = {
                str(li.get("package_status") or "").upper()
                for li in (o.get("line_items") or [])
                if str(li.get("package_status") or "").strip()
            }
            if (order_status in shippable_statuses) or any(ps in shippable_statuses for ps in package_statuses):
                package_ids = [str(p.get("id")) for p in (o.get("packages") or []) if p.get("id") is not None]
                print(
                    "[OrderEngine] TikTok shippable candidate detected: "
                    f"order_id={o.get('id')} order_status={order_status or '-'} "
                    f"package_statuses={sorted(package_statuses) if package_statuses else []} "
                    f"package_ids={package_ids}"
                )
                break

        new_count = 0
        for o in orders:
            order_id = str(o.get("id", ""))
            if not order_id:
                continue
            existing = db.query(Order).filter_by(platform="tiktok", platform_order_id=order_id).first()
            if existing:
                new_status = o.get("status", "unknown").lower()
                new_total = float(o.get("payment", {}).get("total_amount", 0) or 0)
                new_shipping = float(o.get("payment", {}).get("shipping_fee", 0) or 0)
                updated = False
                if existing.status != new_status:
                    existing.status = new_status
                    updated = True
                if existing.total_price != new_total:
                    existing.total_price = new_total
                    updated = True
                if existing.shipping_fee != new_shipping:
                    existing.shipping_fee = new_shipping
                    updated = True
                # Correct any line item unit_prices stored with the old /100 bug
                for item_row in existing.items:
                    for li in o.get("line_items", []):
                        if str(li.get("sku_id", "")) == str(item_row.platform_sku) or \
                           str(li.get("seller_sku", "")) == str(item_row.platform_sku):
                            correct_price = float(li.get("sale_price", 0) or 0)
                            if item_row.unit_price != correct_price:
                                item_row.unit_price = correct_price
                                updated = True
                if updated:
                    db.commit()
                continue

            created_at = None
            try:
                created_at = datetime.fromtimestamp(int(o.get("create_time", 0)))
            except Exception:
                pass

            status_str = o.get("status", "unknown").lower()
            total      = float(o.get("payment", {}).get("total_amount", 0) or 0)
            shipping   = float(o.get("payment", {}).get("shipping_fee", 0) or 0)

            order = Order(
                platform="tiktok",
                platform_order_id=order_id,
                status=status_str,
                buyer_name=o.get("recipient_address", {}).get("name", "Unknown"),
                total_price=total,
                shipping_fee=shipping,
                payment_method=o.get("payment", {}).get("payment_method", ""),
                items_count=len(o.get("line_items", [])),
                notified=False,
                platform_created_at=created_at,
            )
            db.add(order)
            db.flush()

            for item in o.get("line_items", []):
                db.add(OrderItem(
                    order_id=order.id,
                    platform_sku=item.get("seller_sku", ""),
                    product_name=item.get("product_name", ""),
                    quantity=int(item.get("quantity", 1) or 1),
                    unit_price=float(item.get("sale_price", 0) or 0),
                ))

            db.commit()
            new_count += 1

            await self._notify_and_mark(
                db,
                order,
                [{
                    "name": self._resolve_display_name(db, "tiktok", i.platform_sku, i.product_name),
                    "quantity": i.quantity
                } for i in order.items],
            )

        print(f"[OrderEngine] TikTok: {new_count} new orders")
        return new_count

    # ─── Run all ─────────────────────────────────────────────────────────────

    async def fetch_all_orders(self, db: Session):
        results = {}
        try:
            results["lazada"] = await self.fetch_lazada_orders(db)
        except Exception as e:
            print(f"[OrderEngine] Lazada error: {e}")
            results["lazada"] = 0
        try:
            results["tiktok"] = await self.fetch_tiktok_orders(db)
        except Exception as e:
            print(f"[OrderEngine] TikTok error: {e}")
            results["tiktok"] = 0
        try:
            results["shopee"] = await self.fetch_shopee_orders(db)
        except Exception as e:
            print(f"[OrderEngine] Shopee error: {e}")
            results["shopee"] = 0
        try:
            results["retried_notifications"] = await self.retry_missed_notifications(db)
        except Exception as e:
            print(f"[OrderEngine] Notification retry error: {e}")
            results["retried_notifications"] = 0
        return results

    # ─── Shopee ───────────────────────────────────────────────────────────────

    async def fetch_shopee_orders(self, db: Session):
        """Fetch Shopee MY orders via Open API first, browser interception as fallback."""
        try:
            return await self._fetch_shopee_orders_api(db)
        except Exception as e:
            print(f"[OrderEngine] Shopee API fetch failed, falling back to browser: {e}")
            return await self._fetch_shopee_orders_browser(db)

    async def _fetch_shopee_orders_api(self, db: Session):
        raise marketplace_unavailable("Shopee order fetch", "shopee")

        # Keep this list aligned to Shopee Open API accepted enum values.
        # Deprecated/invalid values produce noisy recurring errors and hide coverage gaps.
        statuses = [
            "UNPAID",
            "READY_TO_SHIP",
            "PROCESSED",
            "SHIPPED",
            "TO_CONFIRM_RECEIVED",
            "COMPLETED",
            "CANCELLED",
            "IN_CANCEL",
        ]

        invalid_statuses = set()

        seen_order_sns = set()
        for status in statuses:
            cursor = ""
            loops = 0
            while loops < 20:
                loops += 1
                data = client.get_order_list(
                    time_from=from_ts,
                    time_to=now_ts,
                    order_status=status,
                    cursor=cursor,
                    page_size=50,
                )
                if data.get("error"):
                    msg = str(data.get("message") or "")
                    if "order_status is invalid" in msg.lower():
                        if status not in invalid_statuses:
                            invalid_statuses.add(status)
                            print(
                                f"[OrderEngine] Shopee status skipped (invalid): status={status} message={msg}"
                            )
                    else:
                        print(f"[OrderEngine] Shopee order list error ({status}): {data}")
                    break

                resp = data.get("response", {}) or {}
                rows = resp.get("order_list", []) or []
                for row in rows:
                    sn = str(row.get("order_sn", "")).strip()
                    if sn:
                        seen_order_sns.add(sn)

                if not resp.get("more"):
                    break
                cursor = str(resp.get("next_cursor") or "")
                if not cursor:
                    break

        if invalid_statuses:
            print(
                "[OrderEngine] Shopee status validation summary: "
                f"invalid_statuses={sorted(invalid_statuses)}"
            )

        if not seen_order_sns:
            print("[OrderEngine] Shopee API returned no recent orders")
            return 0

        new_count = 0
        order_sn_list = list(seen_order_sns)
        for i in range(0, len(order_sn_list), 50):
            chunk = order_sn_list[i:i + 50]
            detail = client.get_order_detail(chunk)
            if detail.get("error"):
                print(f"[OrderEngine] Shopee order detail error: {detail}")
                continue

            detail_orders = (detail.get("response", {}) or {}).get("order_list", []) or []
            for o in detail_orders:
                order_sn = str(o.get("order_sn", "")).strip()
                if not order_sn:
                    continue

                existing = db.query(Order).filter_by(platform="shopee", platform_order_id=order_sn).first()
                status = str(o.get("order_status") or o.get("status") or "unknown").lower()
                total = self._to_float(o.get("total_amount") or o.get("pay_amount") or 0, 0.0)
                payment_method = str(o.get("payment_method") or "")

                if existing:
                    updated = False
                    if existing.status != status:
                        existing.status = status
                        updated = True
                    if (existing.total_price or 0.0) != total:
                        existing.total_price = total
                        updated = True
                    if (existing.payment_method or "") != payment_method:
                        existing.payment_method = payment_method
                        updated = True
                    if updated:
                        db.commit()
                    continue

                buyer = str(o.get("buyer_username") or o.get("recipient_address", {}).get("name") or "Unknown")
                created_at = None
                try:
                    created_raw = int(o.get("create_time") or 0)
                    if created_raw > 0:
                        created_at = datetime.fromtimestamp(created_raw)
                except Exception:
                    created_at = None

                items_raw = []
                for item in o.get("item_list", []) or []:
                    qty = int(item.get("model_quantity_purchased") or item.get("quantity_purchased") or item.get("quantity") or 1)
                    price = self._to_float(
                        item.get("model_discounted_price")
                        or item.get("model_original_price")
                        or item.get("item_price")
                        or item.get("price")
                        or 0,
                        0.0,
                    )
                    items_raw.append({
                        "name": str(item.get("item_name") or item.get("name") or ""),
                        "quantity": max(1, qty),
                        "price": price,
                        "sku": str(item.get("model_sku") or item.get("item_sku") or item.get("item_id") or ""),
                    })

                order = Order(
                    platform="shopee",
                    platform_order_id=order_sn,
                    status=status,
                    buyer_name=buyer,
                    total_price=total,
                    shipping_fee=0.0,
                    payment_method=payment_method,
                    items_count=len(items_raw),
                    notified=False,
                    platform_created_at=created_at,
                )
                db.add(order)
                db.flush()

                for item in items_raw:
                    db.add(OrderItem(
                        order_id=order.id,
                        platform_sku=item.get("sku", ""),
                        product_name=item.get("name", ""),
                        quantity=item.get("quantity", 1),
                        unit_price=item.get("price", 0.0),
                    ))

                db.commit()
                new_count += 1

                await self._notify_and_mark(
                    db,
                    order,
                    [{
                        "name": self._resolve_display_name(db, "shopee", item.get("sku", ""), item.get("name", "")),
                        "quantity": item.get("quantity", 1),
                    } for item in items_raw],
                )

        print(f"[OrderEngine] Shopee(API): {new_count} new orders")
        return new_count

    async def _fetch_shopee_orders_browser(self, db: Session):
        """Unsupported Shopee order fetch path."""
        raise marketplace_unavailable("Shopee browser order fetch", "shopee")
