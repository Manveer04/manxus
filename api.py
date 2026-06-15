import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("SHOPEE_TRACKER_DB", "invsync.db"))
if not DB_PATH.is_absolute():
    DB_PATH = PROJECT_ROOT / DB_PATH
URLS_PATH = Path(os.getenv("SHOPEE_TRACKER_URLS_FILE", "urls.txt"))
if not URLS_PATH.is_absolute():
    URLS_PATH = PROJECT_ROOT / URLS_PATH
SCRIPT_PATH = PROJECT_ROOT / "shopee_tracker.py"
LOG_PATH = PROJECT_ROOT / "tracker_sync.log"

app = FastAPI(title="Shopee Tracker Bridge")


class UrlListRequest(BaseModel):
    urls: list[str] = Field(default_factory=list)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL,
            product_name TEXT,
            original_price REAL,
            sale_price REAL,
            shipping_fee REAL,
            seller_name TEXT,
            seller_location TEXT,
            stock_quantity INTEGER,
            scraped_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_price_history_url_ts ON price_history(url, scraped_at DESC)")
    conn.commit()


def _to_float(v):
    try:
        return None if v is None else float(v)
    except Exception:
        return None


def _to_int(v):
    try:
        return None if v is None else int(v)
    except Exception:
        return None


def _recent_rows(limit: int = 5):
    with _connect() as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            """
            SELECT url, product_name, seller_name, seller_location,
                   original_price, sale_price, shipping_fee, stock_quantity, scraped_at
            FROM price_history
            ORDER BY scraped_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            {
                "url": r["url"],
                "product_name": r["product_name"],
                "seller": r["seller_name"],
                "seller_location": r["seller_location"],
                "original_price": _to_float(r["original_price"]),
                "sale_price": _to_float(r["sale_price"]),
                "shipping_fee": _to_float(r["shipping_fee"]),
                "stock_quantity": _to_int(r["stock_quantity"]),
                "scraped_at": r["scraped_at"],
            }
            for r in rows
        ]


@app.get("/products")
def products():
    with _connect() as conn:
        _ensure_schema(conn)
        latest_rows = conn.execute(
            """
            SELECT p1.*
            FROM price_history p1
            INNER JOIN (
                SELECT url, MAX(scraped_at) AS max_scraped_at
                FROM price_history
                GROUP BY url
            ) latest
                ON latest.url = p1.url
               AND latest.max_scraped_at = p1.scraped_at
            ORDER BY p1.scraped_at DESC
            """
        ).fetchall()

        out = []
        for row in latest_rows:
            prev = conn.execute(
                """
                SELECT sale_price, original_price
                FROM price_history
                WHERE url = ? AND scraped_at < ?
                ORDER BY scraped_at DESC
                LIMIT 1
                """,
                (row["url"], row["scraped_at"]),
            ).fetchone()

            curr = _to_float(row["sale_price"]) if row["sale_price"] is not None else _to_float(row["original_price"])
            prev_price = None
            if prev is not None:
                prev_price = _to_float(prev["sale_price"]) if prev["sale_price"] is not None else _to_float(prev["original_price"])

            trend = "flat"
            if prev_price is not None and curr is not None:
                if curr < prev_price:
                    trend = "down"
                elif curr > prev_price:
                    trend = "up"

            out.append(
                {
                    "url": row["url"],
                    "product_name": row["product_name"],
                    "seller": row["seller_name"],
                    "seller_location": row["seller_location"],
                    "original_price": _to_float(row["original_price"]),
                    "sale_price": _to_float(row["sale_price"]),
                    "shipping_fee": _to_float(row["shipping_fee"]),
                    "stock_quantity": _to_int(row["stock_quantity"]),
                    "last_checked": row["scraped_at"],
                    "previous_price": prev_price,
                    "price_drop": bool(prev_price is not None and curr is not None and curr < prev_price),
                    "trend": trend,
                }
            )
        return out


@app.get("/history")
def history(url: str = Query(..., min_length=8)):
    with _connect() as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            """
            SELECT url, product_name, seller_name, seller_location,
                   original_price, sale_price, shipping_fee, stock_quantity, scraped_at
            FROM price_history
            WHERE url = ?
            ORDER BY scraped_at ASC
            """,
            (url,),
        ).fetchall()
        return [
            {
                "url": r["url"],
                "product_name": r["product_name"],
                "seller": r["seller_name"],
                "seller_location": r["seller_location"],
                "original_price": _to_float(r["original_price"]),
                "sale_price": _to_float(r["sale_price"]),
                "shipping_fee": _to_float(r["shipping_fee"]),
                "stock_quantity": _to_int(r["stock_quantity"]),
                "scraped_at": r["scraped_at"],
            }
            for r in rows
        ]


@app.get("/urls")
def get_urls():
    if not URLS_PATH.exists():
        return {"urls": []}
    lines = [ln.strip() for ln in URLS_PATH.read_text(encoding="utf-8").splitlines()]
    urls = [ln for ln in lines if ln and not ln.startswith("#") and (ln.startswith("http://") or ln.startswith("https://"))]
    return {"urls": urls}


@app.post("/urls")
def save_urls(body: UrlListRequest):
    cleaned = []
    seen = set()
    for raw in body.urls:
        value = (raw or "").strip()
        if not (value.startswith("http://") or value.startswith("https://")):
            continue
        if value in seen:
            continue
        seen.add(value)
        cleaned.append(value)

    URLS_PATH.parent.mkdir(parents=True, exist_ok=True)
    URLS_PATH.write_text("\n".join(cleaned) + ("\n" if cleaned else ""), encoding="utf-8")
    return {"status": "ok", "count": len(cleaned)}


@app.post("/sync")
def sync():
    if not SCRIPT_PATH.exists():
        raise HTTPException(status_code=404, detail="shopee_tracker.py not found")

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as log_file:
        log_file.write(f"\n=== {datetime.utcnow().isoformat()} UTC sync start ({SCRIPT_PATH}) ===\n")
        log_file.flush()
        process = subprocess.Popen(
            [sys.executable, str(SCRIPT_PATH)],
            cwd=str(PROJECT_ROOT),
            stdout=log_file,
            stderr=log_file,
            text=True,
        )

    try:
        return_code = process.wait(timeout=300)
    except subprocess.TimeoutExpired:
        return {"status": "syncing", "pid": process.pid, "log": str(LOG_PATH), "message": "Sync is still running."}

    if return_code != 0:
        tail = ""
        try:
            tail = LOG_PATH.read_text(encoding="utf-8")[-4000:]
        except Exception:
            pass
        raise HTTPException(status_code=500, detail={"message": "Tracker sync failed", "log": str(LOG_PATH), "tail": tail})

    recent = _recent_rows(5)
    return {"status": "done", "pid": process.pid, "log": str(LOG_PATH), "captured": len(recent), "recent": recent}
