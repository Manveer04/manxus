"""
Email watcher — monitors Yahoo Mail via IMAP IDLE.
Persists seen email UIDs in SQLite so restarts don't re-trigger old emails.

First run (empty DB): marks all existing emails as seen, no order checks fired.
Subsequent runs: only new UIDs trigger order fetches.
Multiple new emails for same platform = one order check, not one per email.

Setup:
  YAHOO_EMAIL=your@yahoo.com
  YAHOO_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx  (Yahoo App Password, not real password)
"""
import asyncio
import email
import imaplib
import logging
import os
import socket
import sqlite3
import time
from email.header import decode_header
from pathlib import Path

log = logging.getLogger("email_watcher")
if not log.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [EmailWatcher] %(message)s"))
    log.addHandler(_handler)
log.setLevel(logging.INFO)
log.propagate = False

# ── Config ─────────────────────────────────────────────────────────────────────
YAHOO_EMAIL    = os.getenv("YAHOO_EMAIL", "")
YAHOO_PASSWORD = os.getenv("YAHOO_APP_PASSWORD", "")
IMAP_HOST      = "imap.mail.yahoo.com"
IMAP_PORT      = 993
IDLE_TIMEOUT   = 25 * 60  # renew before Yahoo's ~28 min limit

DB_PATH = Path("/app/data/email_watcher.db")

PLATFORM_SENDERS = {
    "info@mail.shopee.com.my":       "shopee",
    "noreply@support.lazada.com.my": "lazada",
    "sellersupport@shop.tiktok.com": "tiktok",
}

PLATFORM_SUBJECT_FILTERS = {
    "shopee": ["order #"],
    "lazada": ["new order"],
    "tiktok": ["order to ship", "orders to ship"],
}


# ── Persistent UID store ───────────────────────────────────────────────────────

def _db_connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_emails (
            platform TEXT NOT NULL,
            uid      TEXT NOT NULL,
            subject  TEXT,
            seen_at  TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (platform, uid)
        )
    """)
    conn.commit()
    return conn


def _is_first_run(platform: str) -> bool:
    """True if no UIDs have ever been stored for this platform."""
    conn = _db_connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM seen_emails WHERE platform=?", (platform,)
        ).fetchone()
        return row[0] == 0
    finally:
        conn.close()


def _is_seen(platform: str, uid: str) -> bool:
    conn = _db_connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM seen_emails WHERE platform=? AND uid=?", (platform, uid)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _mark_seen(platform: str, uid: str, subject: str = ""):
    conn = _db_connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO seen_emails (platform, uid, subject) VALUES (?,?,?)",
            (platform, uid, subject)
        )
        conn.commit()
    finally:
        conn.close()


# ── IMAP helpers ───────────────────────────────────────────────────────────────

def _connect() -> imaplib.IMAP4_SSL:
    imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    imap.login(YAHOO_EMAIL, YAHOO_PASSWORD)
    imap.select("INBOX")
    log.info("[EmailWatcher] Connected to Yahoo IMAP")
    return imap


def _get_subject(imap: imaplib.IMAP4_SSL, uid: bytes) -> str:
    try:
        _, data = imap.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (SUBJECT)])")
        if not data or not data[0]:
            return ""
        msg   = email.message_from_bytes(data[0][1])
        parts = decode_header(msg.get("Subject", ""))
        subject = ""
        for part, enc in parts:
            if isinstance(part, bytes):
                subject += part.decode(enc or "utf-8", errors="replace")
            else:
                subject += part
        return subject.strip()
    except Exception:
        return ""


def _check_new_emails(imap: imaplib.IMAP4_SSL) -> dict[str, bool]:
    """
    Scan inbox for each platform sender.

    First run per platform: seed all existing UIDs as seen, return no triggers.
    Subsequent runs: return {platform: True} only for platforms with genuinely new emails.
    Multiple new emails for the same platform = still just one True entry.
    """
    triggers: dict[str, bool] = {}

    try:
        for sender, platform in PLATFORM_SENDERS.items():
            # Search ALL emails from this sender (not just UNSEEN)
            # so first-run seeding works even for already-read emails
            _, data = imap.uid("search", None, f'FROM "{sender}"')
            if not data or not data[0]:
                continue

            all_uids = data[0].split()
            first_run = _is_first_run(platform)

            if first_run:
                # Seed all existing UIDs — no triggers fired
                count = 0
                for uid in all_uids:
                    uid_str = uid.decode()
                    subject = _get_subject(imap, uid)
                    _mark_seen(platform, uid_str, subject)
                    count += 1
                log.info(
                    f"[EmailWatcher] First run for {platform} — "
                    f"seeded {count} existing emails, no order check triggered"
                )
                continue

            # Normal run — look for UIDs not in DB yet
            for uid in all_uids:
                uid_str = uid.decode()
                if _is_seen(platform, uid_str):
                    continue

                subject = _get_subject(imap, uid)
                subject_lower = subject.lower()

                # Subject filter — skip OTPs, promos, etc.
                filters = PLATFORM_SUBJECT_FILTERS.get(platform, [])
                if filters and not any(f in subject_lower for f in filters):
                    log.debug(f"[EmailWatcher] Skipping non-order email ({platform}): {subject[:60]}")
                    _mark_seen(platform, uid_str, subject)
                    continue

                log.info(f"[EmailWatcher] 📧 New {platform} order email: {subject[:60]}")
                _mark_seen(platform, uid_str, subject)
                # One trigger per platform regardless of how many new emails
                triggers[platform] = True

    except Exception as e:
        log.error(f"[EmailWatcher] Error checking emails: {e}")

    return triggers


def _idle(imap: imaplib.IMAP4_SSL, timeout: int) -> bool:
    """
    IMAP IDLE — blocks until server signals new mail or timeout.
    Returns True if new mail arrived.
    """
    try:
        tag = imap._new_tag().decode()
        imap.send(f"{tag} IDLE\r\n".encode())
        imap.readline()  # "+" continuation

        imap.sock.settimeout(timeout)
        start    = time.time()
        new_mail = False

        while True:
            try:
                line = imap.readline().decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                log.debug(f"[EmailWatcher] IDLE: {line}")
                if "EXISTS" in line or "RECENT" in line:
                    new_mail = True
                    break
                if time.time() - start >= timeout:
                    break
            except socket.timeout:
                break

        imap.send(b"DONE\r\n")
        imap.readline()  # tagged OK
        imap.sock.settimeout(None)
        return new_mail

    except Exception as e:
        log.warning(f"[EmailWatcher] IDLE error: {e}")
        return False


# ── Order trigger ──────────────────────────────────────────────────────────────

async def _trigger_order_fetch(platform: str, db_factory):
    from app.order_engine import OrderEngine
    engine = OrderEngine()
    db     = db_factory()
    try:
        log.info(f"[EmailWatcher] 🚀 Triggering order fetch for {platform}")
        if platform == "shopee":
            result = await engine.fetch_shopee_orders(db)
        elif platform == "lazada":
            result = await engine.fetch_lazada_orders(db)
        elif platform == "tiktok":
            result = await engine.fetch_tiktok_orders(db)
        else:
            result = await engine.fetch_all_orders(db)
        log.info(f"[EmailWatcher] ✓ Order fetch done for {platform}: {result}")
    except Exception as e:
        log.error(f"[EmailWatcher] Order fetch failed for {platform}: {e}")
    finally:
        db.close()


# ── Main loop ──────────────────────────────────────────────────────────────────

async def run_email_watcher(db_factory):
    """
    Main IMAP IDLE loop. Runs forever, auto-reconnects on error.
    Start as: asyncio.create_task(run_email_watcher(SessionLocal))
    """
    if not YAHOO_EMAIL or not YAHOO_PASSWORD:
        log.warning("[EmailWatcher] YAHOO_EMAIL or YAHOO_APP_PASSWORD not set — watcher disabled")
        return

    log.info(f"[EmailWatcher] Starting — watching {YAHOO_EMAIL} via IMAP IDLE")
    log.info(f"[EmailWatcher] UID store: {DB_PATH}")

    while True:
        imap = None
        try:
            imap = _connect()

            # On connect: check for any emails that arrived while we were offline
            triggers = _check_new_emails(imap)
            for platform in triggers:
                asyncio.create_task(_trigger_order_fetch(platform, db_factory))

            # IDLE loop
            while True:
                log.debug("[EmailWatcher] Entering IDLE...")
                new_mail = await asyncio.get_event_loop().run_in_executor(
                    None, _idle, imap, IDLE_TIMEOUT
                )

                if new_mail:
                    log.info("[EmailWatcher] 🔔 New mail signal — checking...")
                    await asyncio.sleep(2)  # let email fully land
                    triggers = _check_new_emails(imap)
                    for platform in triggers:
                        asyncio.create_task(_trigger_order_fetch(platform, db_factory))
                else:
                    log.debug("[EmailWatcher] IDLE timeout — renewing connection")
                    imap.noop()

        except imaplib.IMAP4.error as e:
            log.error(f"[EmailWatcher] IMAP error: {e} — reconnecting in 30s")
            await asyncio.sleep(30)
        except Exception as e:
            log.error(f"[EmailWatcher] Unexpected error: {e} — reconnecting in 60s")
            await asyncio.sleep(60)
        finally:
            if imap:
                try:
                    imap.logout()
                except Exception:
                    pass