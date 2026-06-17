from datetime import datetime
import logging
import os
import time
import sqlite3
import subprocess
import sys
import binascii
import re
from typing import List, Optional, Literal
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy import func
from pathlib import Path
import base64

from app.database import get_db
from app.models import Product, ProductGroup, PlatformListing, SyncLog, Order, OrderItem
from app.notifier import notify_new_order
from app.order_engine import OrderEngine
from app.scrapers import SCRAPERS
from app.scrapers.base import SESSIONS_DIR
from app.scrapers.lazada import lazada_arrange_shipment, lazada_create_awb, lazada_get_awb_result
from app.scrapers.shopee_api import (
    shopee_arrange_shipment,
    shopee_create_awb,
    shopee_get_awb_result,
    shopee_get_awb_parameter,
    shopee_get_shipping_parameter,
    shopee_get_tracking_number,
)
from app.scrapers.tiktok import (
    TikTokAwbStateConflictError,
    tiktok_arrange_shipment,
    tiktok_create_awb,
    tiktok_get_awb_result,
)
from app.sync_engine import SyncEngine
from app.sync_log_utils import get_latest_sync_log, get_recent_sync_logs

router = APIRouter(prefix="/api")
engine = SyncEngine()
order_engine = OrderEngine()
logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent.parent / "static"
IMAGES_DIR = Path("/app/data/images")
IMAGES_DIR.mkdir(parents=True, exist_ok=True)
# Fallback for development (Windows paths)
if not IMAGES_DIR.exists():
    try:
        IMAGES_DIR = Path("data/images")
        IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    except:
        pass

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TRACKER_DB_PATH = Path(os.getenv("SHOPEE_TRACKER_DB", "invsync.db"))
if not TRACKER_DB_PATH.is_absolute():
    TRACKER_DB_PATH = PROJECT_ROOT / TRACKER_DB_PATH
TRACKER_URLS_PATH = Path(os.getenv("SHOPEE_TRACKER_URLS_FILE", "urls.txt"))
if not TRACKER_URLS_PATH.is_absolute():
    TRACKER_URLS_PATH = PROJECT_ROOT / TRACKER_URLS_PATH
TRACKER_SCRIPT_PATHS = [
    PROJECT_ROOT / "shopee_tracker.py",
    Path("/app/shopee_tracker.py"),
]
TRACKER_LOG_PATH = PROJECT_ROOT / "tracker_sync.log"


def _tracker_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(TRACKER_DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_tracker_schema(conn: sqlite3.Connection) -> None:
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
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_price_history_url_ts ON price_history(url, scraped_at DESC)"
    )
    conn.commit()


def _safe_float(value):
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _safe_int(value):
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        return None


def _tracker_cookies_file() -> Path:
    path = Path(os.getenv("SHOPEE_TRACKER_COOKIES_FILE", "shopee_cookies.json"))
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def _tracker_cookies_exist() -> bool:
    cookies_file = _tracker_cookies_file()
    return cookies_file.exists() and cookies_file.stat().st_size > 0


def _tracker_recent_rows(limit: int = 5) -> list[dict[str, object]]:
    with _tracker_connect() as conn:
        _ensure_tracker_schema(conn)
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
                "original_price": _safe_float(r["original_price"]),
                "sale_price": _safe_float(r["sale_price"]),
                "shipping_fee": _safe_float(r["shipping_fee"]),
                "stock_quantity": _safe_int(r["stock_quantity"]),
                "scraped_at": r["scraped_at"],
            }
            for r in rows
        ]

# ── Schemas ───────────────────────────────────────────────────────────────────
class ListingOut(BaseModel):
    id: int
    platform: str
    platform_sku: str
    platform_product_id: Optional[str]
    current_stock: int
    price: float
    sync_status: str
    last_synced: Optional[datetime]
    error_message: Optional[str]
    class Config: from_attributes = True

class ProductOut(BaseModel):
    id: int
    master_sku: str
    name: str
    master_stock: int
    backorder_display_qty: int
    image_url: Optional[str]
    auto_sync: bool
    listings: List[ListingOut]
    updated_at: Optional[datetime]
    class Config: from_attributes = True

class GroupMemberOut(BaseModel):
    id: int
    master_sku: str
    name: str
    listings: List[ListingOut]
    class Config: from_attributes = True

class GroupOut(BaseModel):
    id: int
    display_name: str
    master_stock: int
    backorder_display_qty: int
    image_url: Optional[str]
    members: List[GroupMemberOut]
    updated_at: Optional[datetime]
    class Config: from_attributes = True

class UpdateStockRequest(BaseModel):
    new_stock: int = Field(ge=0, le=1_000_000)
    platforms: Optional[List[Literal["shopee", "shopee_sg", "lazada", "tiktok"]]] = None

class UpdateProductRequest(BaseModel):
    name: Optional[str] = None
    master_stock: Optional[int] = Field(default=None, ge=0, le=1_000_000)
    auto_sync: Optional[bool] = None
    backorder_display_qty: Optional[int] = Field(default=None, ge=0, le=1_000_000)

class CreateGroupRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)
    product_ids: List[int]
    master_stock: int = Field(default=0, ge=0, le=1_000_000)

class UpdateGroupRequest(BaseModel):
    display_name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    master_stock: Optional[int] = Field(default=None, ge=0, le=1_000_000)
    backorder_display_qty: Optional[int] = Field(default=None, ge=0, le=1_000_000)
    product_ids: Optional[List[int]] = None

class ImageUploadRequest(BaseModel):
    image_data: str = Field(min_length=32, max_length=8_000_000)

class ShopeeShipmentRequest(BaseModel):
    package_number: Optional[str] = None
    pickup: Optional[dict] = None
    dropoff: Optional[dict] = None
    non_integrated: Optional[dict] = None

class ShopeeAwbRequest(BaseModel):
    package_number: Optional[str] = None
    shipping_document_type: Optional[str] = None
    wait_seconds: Optional[int] = Field(default=20, ge=0, le=180)
    poll_seconds: Optional[int] = Field(default=2, ge=1, le=30)

class LazadaAwbRequest(BaseModel):
    wait_seconds: Optional[int] = Field(default=20, ge=0, le=180)
    poll_seconds: Optional[int] = Field(default=2, ge=1, le=30)

class LazadaShipmentRequest(BaseModel):
    delivery_type: str = Field(min_length=1, max_length=64)
    shipping_provider: str = Field(min_length=1, max_length=64)
    tracking_number: str = Field(min_length=1, max_length=128)
    order_item_ids: Optional[List[str]] = None

class TikTokShipmentRequest(BaseModel):
    package_id: Optional[str] = None

class TikTokAwbRequest(BaseModel):
    package_id: Optional[str] = None
    wait_seconds: Optional[int] = Field(default=20, ge=0, le=180)
    poll_seconds: Optional[int] = Field(default=2, ge=1, le=30)

class SyncLogOut(BaseModel):
    id: int
    platform: str
    action: str
    old_stock: Optional[int]
    new_stock: Optional[int]
    message: Optional[str]
    created_at: datetime
    class Config: from_attributes = True


class TrackerUrlListRequest(BaseModel):
    urls: List[str] = Field(default_factory=list)


def _notification_base_url() -> str:
    raw = os.getenv("ORDER_ACTION_BASE_URL", os.getenv("PUBLIC_BASE_URL", "http://192.168.50.129:8080")).rstrip("/")
    return raw


def _read_tracker_urls_file() -> List[str]:
    if not TRACKER_URLS_PATH.exists():
        return []
    out = []
    for line in TRACKER_URLS_PATH.read_text(encoding="utf-8").splitlines():
        v = line.strip()
        if not v or v.startswith("#"):
            continue
        if v.startswith("http://") or v.startswith("https://"):
            out.append(v)
    return out


def _require_known_platform(platform: str) -> str:
    p = (platform or "").strip().lower()
    if p not in SCRAPERS:
        raise HTTPException(400, f"Unknown platform: {platform}")
    return p


async def _send_test_notification(platform: str) -> dict:
    order_id = f"TEST-{platform.upper()}-{int(time.time())}"
    action_url = f"{_notification_base_url()}/orders-admin?platform={platform}&order={order_id}"
    ok = await notify_new_order(
        platform=platform,
        order_id=order_id,
        buyer="Test Buyer",
        total=12.34,
        items=[
            {"name": "Test Product A", "quantity": 1},
            {"name": "Test Product B", "quantity": 2},
        ],
        action_url=action_url,
    )
    return {
        "platform": platform,
        "ok": ok,
        "test_order_id": order_id,
        "action_url": action_url,
    }

# ── Products ──────────────────────────────────────────────────────────────────
@router.get("/products", response_model=List[ProductOut])
def list_products(db: Session = Depends(get_db)):
    return db.query(Product).all()

@router.get("/products/{product_id}", response_model=ProductOut)
def get_product(product_id: int, db: Session = Depends(get_db)):
    p = db.query(Product).filter_by(id=product_id).first()
    if not p: raise HTTPException(404, "Product not found")
    return p

@router.patch("/products/{product_id}", response_model=ProductOut)
def update_product(product_id: int, body: UpdateProductRequest, db: Session = Depends(get_db)):
    p = db.query(Product).filter_by(id=product_id).first()
    if not p: raise HTTPException(404, "Product not found")
    if body.name is not None: p.name = body.name
    if body.master_stock is not None:
        p.master_stock = body.master_stock
        for listing in p.listings:
            listing.sync_status = "out_of_sync"
    if body.auto_sync is not None: p.auto_sync = body.auto_sync
    if body.backorder_display_qty is not None:
        p.backorder_display_qty = max(0, body.backorder_display_qty)
        # If enabling backorder mode and stock is already <= 0, mark out_of_sync
        # so the next push sends the display qty
        if p.master_stock <= 0:
            for listing in p.listings:
                listing.sync_status = "out_of_sync"
    db.commit(); db.refresh(p)
    return p

@router.get("/groups", response_model=List[GroupOut])
def list_groups(db: Session = Depends(get_db)):
    return db.query(ProductGroup).order_by(ProductGroup.display_name.asc()).all()


@router.post("/groups", response_model=GroupOut)
def create_group(body: CreateGroupRequest, db: Session = Depends(get_db)):
    if not body.product_ids:
        raise HTTPException(400, "product_ids must not be empty")

    unique_ids = list(dict.fromkeys(body.product_ids))
    products = db.query(Product).filter(Product.id.in_(unique_ids)).all()
    if len(products) != len(unique_ids):
        raise HTTPException(400, "One or more product_ids are invalid")

    group = ProductGroup(
        display_name=body.display_name.strip(),
        master_stock=body.master_stock,
    )
    group.members = products
    db.add(group)
    db.commit()
    db.refresh(group)
    return group


@router.get("/groups/suggest")
def suggest_groups(db: Session = Depends(get_db)):
    def tokens(value: str) -> set[str]:
        return set(re.findall(r"[a-z0-9]+", (value or "").lower()))

    def similarity(a: str, b: str) -> float:
        ta, tb = tokens(a), tokens(b)
        if not ta or not tb:
            return 0.0
        return len(ta & tb) / min(len(ta), len(tb))

    ungrouped = db.query(Product).filter(~Product.groups.any()).all()

    threshold = 0.45
    used: set[int] = set()
    suggestions = []
    for p in ungrouped:
        if p.id in used:
            continue
        group = [p]
        used.add(p.id)
        for q in ungrouped:
            if q.id in used or q.id == p.id:
                continue
            if similarity(p.name, q.name) >= threshold:
                group.append(q)
                used.add(q.id)
        if len(group) < 2:
            continue
        best = min(group, key=lambda x: len(x.name))
        suggestions.append(
            {
                "suggested_name": best.name[:60],
                "products": [
                    {
                        "id": g.id,
                        "name": g.name,
                        "master_sku": g.master_sku,
                        "listings": [
                            {
                                "platform": l.platform,
                                "platform_sku": l.platform_sku,
                                "current_stock": l.current_stock,
                            }
                            for l in g.listings
                        ],
                    }
                    for g in group
                ],
            }
        )
    return suggestions


@router.get("/groups/{group_id}", response_model=GroupOut)
def get_group(group_id: int, db: Session = Depends(get_db)):
    g = db.query(ProductGroup).filter_by(id=group_id).first()
    if not g: raise HTTPException(404, "Group not found")
    return g

@router.patch("/groups/{group_id}", response_model=GroupOut)
def update_group(group_id: int, body: UpdateGroupRequest, db: Session = Depends(get_db)):
    g = db.query(ProductGroup).filter_by(id=group_id).first()
    if not g: raise HTTPException(404, "Group not found")
    if body.display_name is not None: g.display_name = body.display_name
    if body.master_stock is not None: g.master_stock = body.master_stock
    if body.backorder_display_qty is not None: g.backorder_display_qty = max(0, body.backorder_display_qty)
    if body.product_ids is not None:
        if not body.product_ids:
            raise HTTPException(400, "product_ids must not be empty")
        products = db.query(Product).filter(Product.id.in_(body.product_ids)).all()
        if len(products) != len(set(body.product_ids)):
            raise HTTPException(400, "One or more product_ids are invalid")
        g.members = products
    db.commit(); db.refresh(g)
    return g

@router.delete("/groups/{group_id}")
def delete_group(group_id: int, db: Session = Depends(get_db)):
    g = db.query(ProductGroup).filter_by(id=group_id).first()
    if not g: raise HTTPException(404, "Group not found")
    db.delete(g); db.commit()
    return {"status": "deleted"}

@router.post("/groups/{group_id}/image")
def upload_group_image(group_id: int, body: ImageUploadRequest, db: Session = Depends(get_db)):
    g = db.query(ProductGroup).filter_by(id=group_id).first()
    if not g: raise HTTPException(404, "Group not found")
    try:
        header, data = body.image_data.split(",", 1) if "," in body.image_data else ("", body.image_data)
        if header and not header.lower().startswith("data:image/"):
            raise HTTPException(400, "Invalid image payload")
        ext = "png" if "png" in header else "webp" if "webp" in header else "jpg"
        try:
            img_bytes = base64.b64decode(data, validate=True)
        except binascii.Error:
            raise HTTPException(400, "Invalid base64 image payload")
        if len(img_bytes) > 5 * 1024 * 1024:
            raise HTTPException(400, "Image too large")
        img_path = IMAGES_DIR / f"group_{group_id}.{ext}"
        img_path.write_bytes(img_bytes)
        g.image_url = f"/api/groups/{group_id}/image/file"
        db.commit()
        return {"status": "ok", "image_url": g.image_url}
    except Exception as e:
        raise HTTPException(400, "Image upload failed")

@router.get("/groups/{group_id}/image/file")
def get_group_image(group_id: int):
    for ext in ["jpg", "png", "webp"]:
        path = IMAGES_DIR / f"group_{group_id}.{ext}"
        if path.exists():
            return FileResponse(str(path))
    raise HTTPException(404, "No image found")

@router.post("/groups/{group_id}/push")
async def push_group_stock(group_id: int, body: UpdateStockRequest, db: Session = Depends(get_db)):
    g = db.query(ProductGroup).filter_by(id=group_id).first()
    if not g: raise HTTPException(404, "Group not found")
    g.master_stock = body.new_stock
    db.commit()
    # Resolve group-level BDQ so all member products get the right platform qty
    grp_bdq = g.backorder_display_qty or 0
    bdq_override = grp_bdq if (body.new_stock <= 0 and grp_bdq > 0) else None
    results = {}
    for product in g.members:
        # Build per-product platform target: only push to platforms this product
        # actually has listings for. shopee and shopee_sg are treated as one selection.
        product_platforms = {l.platform for l in product.listings}
        if body.platforms:
            requested = set(body.platforms)
            # treat shopee and shopee_sg as a linked pair
            if 'shopee' in requested or 'shopee_sg' in requested:
                requested |= {'shopee', 'shopee_sg'}
            target = [p for p in product_platforms if p in requested]
            if not target:
                continue  # this member has no listings for the requested platforms
        else:
            target = None  # push to all of this member's platforms
        r = await engine.push_product(product.id, body.new_stock, target, db, bdq_override=bdq_override)
        results[product.id] = r
    return {"status": "done", "results": results}

# ── Sync ──────────────────────────────────────────────────────────────────────
@router.post("/products/{product_id}/push")
async def push_stock(product_id: int, body: UpdateStockRequest, db: Session = Depends(get_db)):
    target_platforms = body.platforms
    if target_platforms:
        requested = set(target_platforms)
        # Treat Shopee MY/SG as a linked selection in product-level pushes.
        if "shopee" in requested or "shopee_sg" in requested:
            requested |= {"shopee", "shopee_sg"}
        target_platforms = list(requested)

    result = await engine.push_product(product_id, body.new_stock, target_platforms, db)
    return {"status": "done", "results": result}

@router.post("/sync/pull-all")
async def pull_all(db: Session = Depends(get_db)):
    results = await engine.pull_all(db)
    return {"status": "done", "results": results}

@router.post("/sync/pull/{platform}")
async def pull_single(platform: str, db: Session = Depends(get_db)):
    platform = _require_known_platform(platform)
    ScraperClass = SCRAPERS.get(platform)
    result = await engine.pull_platform(platform, ScraperClass, db)
    await engine._propagate_sales(db)
    return {"status": "done", "results": result}

@router.post("/sync/push-out-of-sync")
async def push_out_of_sync(db: Session = Depends(get_db)):
    return await engine.push_all_out_of_sync(db)


# ── Shopee Tracker ────────────────────────────────────────────────────────────
@router.get("/tracker/products")
def tracker_products():
    with _tracker_connect() as conn:
        _ensure_tracker_schema(conn)
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

        products = []
        for row in latest_rows:
            prev = conn.execute(
                """
                SELECT sale_price, original_price, scraped_at
                FROM price_history
                WHERE url = ? AND scraped_at < ?
                ORDER BY scraped_at DESC
                LIMIT 1
                """,
                (row["url"], row["scraped_at"]),
            ).fetchone()

            sparkline_rows = conn.execute(
                """
                SELECT sale_price, original_price, scraped_at
                FROM price_history
                WHERE url = ?
                ORDER BY scraped_at DESC
                LIMIT 12
                """,
                (row["url"],),
            ).fetchall()
            sparkline_values = []
            for sp in reversed(sparkline_rows):
                val = _safe_float(sp["sale_price"])
                if val is None:
                    val = _safe_float(sp["original_price"])
                if val is not None:
                    sparkline_values.append(val)

            current_price = _safe_float(row["sale_price"])
            if current_price is None:
                current_price = _safe_float(row["original_price"])

            previous_price = None
            if prev is not None:
                previous_price = _safe_float(prev["sale_price"])
                if previous_price is None:
                    previous_price = _safe_float(prev["original_price"])

            price_drop = bool(
                previous_price is not None
                and current_price is not None
                and current_price < previous_price
            )
            trend = "flat"
            if previous_price is not None and current_price is not None:
                if current_price < previous_price:
                    trend = "down"
                elif current_price > previous_price:
                    trend = "up"

            products.append(
                {
                    "url": row["url"],
                    "product_name": row["product_name"],
                    "seller": row["seller_name"],
                    "seller_location": row["seller_location"],
                    "original_price": _safe_float(row["original_price"]),
                    "sale_price": _safe_float(row["sale_price"]),
                    "shipping_fee": _safe_float(row["shipping_fee"]),
                    "stock_quantity": _safe_int(row["stock_quantity"]),
                    "last_checked": row["scraped_at"],
                    "previous_price": previous_price,
                    "price_drop": price_drop,
                    "trend": trend,
                    "sparkline": sparkline_values,
                }
            )
        return products


@router.get("/tracker/history")
def tracker_history(url: str = Query(..., min_length=8)):
    with _tracker_connect() as conn:
        _ensure_tracker_schema(conn)
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
                "original_price": _safe_float(r["original_price"]),
                "sale_price": _safe_float(r["sale_price"]),
                "shipping_fee": _safe_float(r["shipping_fee"]),
                "stock_quantity": _safe_int(r["stock_quantity"]),
                "scraped_at": r["scraped_at"],
            }
            for r in rows
        ]


@router.get("/tracker/urls")
def tracker_urls():
    urls = _read_tracker_urls_file()
    return {"urls": urls}


@router.post("/tracker/urls")
def save_tracker_urls(body: TrackerUrlListRequest):
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
    TRACKER_URLS_PATH.parent.mkdir(parents=True, exist_ok=True)
    TRACKER_URLS_PATH.write_text("\n".join(cleaned) + ("\n" if cleaned else ""), encoding="utf-8")
    return {"status": "ok", "count": len(cleaned)}


@router.post("/tracker/sync")
def tracker_sync():
    if not _tracker_cookies_exist():
        raise HTTPException(
            409,
            detail={
                "message": "shopee_cookies.json is missing. Add the saved cookie file before syncing.",
                "cookies_file": str(_tracker_cookies_file()),
            },
        )

    script_path = next((path for path in TRACKER_SCRIPT_PATHS if path.exists()), None)
    if script_path is None:
        raise HTTPException(404, "No tracker entrypoint found in the container")

    TRACKER_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with TRACKER_LOG_PATH.open("a", encoding="utf-8") as log_file:
        log_file.write(f"\n=== {datetime.utcnow().isoformat()} UTC sync start ({script_path}) ===\n")
        log_file.flush()
        process = subprocess.Popen(
            [sys.executable, str(script_path)],
            cwd=str(PROJECT_ROOT),
            stdout=log_file,
            stderr=log_file,
            text=True,
        )

    try:
        return_code = process.wait(timeout=300)
    except subprocess.TimeoutExpired:
        return {
            "status": "syncing",
            "pid": process.pid,
            "entrypoint": str(script_path),
            "log": str(TRACKER_LOG_PATH),
            "message": "Sync is still running in the background; check the log file for progress.",
        }

    if return_code != 0:
        log_tail = ""
        try:
            log_tail = TRACKER_LOG_PATH.read_text(encoding="utf-8")[-4000:]
        except Exception:
            pass
        raise HTTPException(
            500,
            detail={
                "message": "Tracker sync failed",
                "entrypoint": str(script_path),
                "log": str(TRACKER_LOG_PATH),
                "tail": log_tail,
            },
        )

    return {
        "status": "done",
        "pid": process.pid,
        "entrypoint": str(script_path),
        "log": str(TRACKER_LOG_PATH),
        "captured": len(_tracker_recent_rows(5)),
        "recent": _tracker_recent_rows(5),
    }
 
# ── Platform status ───────────────────────────────────────────────────────────
@router.get("/platforms/status")
async def platform_status(db: Session = Depends(get_db)):
    statuses = {}
    for platform in SCRAPERS.keys():
        if platform == "lazada":
            # Lazada can have both API token and browser session state files.
            token_file = SESSIONS_DIR / "lazada.json"
            browser_file = SESSIONS_DIR / "lazada_browser.json"
            has_session = token_file.exists() or browser_file.exists()
        else:
            session_file = SESSIONS_DIR / f"{platform}.json"
            has_session = session_file.exists()
        statuses[platform] = {"has_session": has_session, "last_sync": None}
        last_log = get_latest_sync_log(db, platform=platform, action="read")
        if last_log:
            statuses[platform]["last_sync"] = last_log.created_at
    return statuses

@router.delete("/platforms/{platform}/session")
def clear_session(platform: str):
    platform = _require_known_platform(platform)
    if platform == "lazada":
        for name in ("lazada.json", "lazada_browser.json"):
            p = SESSIONS_DIR / name
            if p.exists():
                p.unlink()
    else:
        session_file = SESSIONS_DIR / f"{platform}.json"
        if session_file.exists():
            session_file.unlink()
    return {"status": "session cleared", "platform": platform}

# ── Logs & Stats ──────────────────────────────────────────────────────────────
@router.get("/logs", response_model=List[SyncLogOut])
def get_logs(limit: int = Query(default=50, ge=1, le=500), db: Session = Depends(get_db)):
    return get_recent_sync_logs(db, limit=limit)

@router.get("/stats")
def get_stats(db: Session = Depends(get_db)):
    return {
        "total_products":  db.query(Product).count(),
        "total_groups":    db.query(ProductGroup).count(),
        "out_of_sync":     db.query(PlatformListing).filter_by(sync_status="out_of_sync").count(),
        "errors":          db.query(PlatformListing).filter_by(sync_status="error").count(),
        "low_stock":       db.query(Product).filter(Product.master_stock <= 5, Product.master_stock > 0).count(),
        "backorder":       db.query(Product).filter(Product.master_stock <= 0, Product.backorder_display_qty > 0).count(),
    }

# ── Orders ────────────────────────────────────────────────────────────────────
@router.post("/orders/fetch")
async def fetch_orders(db: Session = Depends(get_db)):
    """Manually trigger an order fetch from all platforms. Use this to test."""
    results = await order_engine.fetch_all_orders(db)
    return {"status": "done", "new_orders": results}

@router.post("/orders/fetch/{platform}")
async def fetch_orders_platform(platform: str, db: Session = Depends(get_db)):
    """Trigger an order fetch for a single platform (lazada / tiktok / shopee)."""
    try:
        if platform == "tiktok":
            count = await order_engine.fetch_tiktok_orders(db)
        elif platform == "lazada":
            count = await order_engine.fetch_lazada_orders(db)
        elif platform == "shopee":
            count = await order_engine.fetch_shopee_orders(db)
        else:
            raise HTTPException(400, "Unknown platform")
    except ModuleNotFoundError:
        raise _scrapers_disabled_error(f"Order fetch for {platform}")
    retried = await order_engine.retry_missed_notifications(db)
    return {"platform": platform, "new_orders": count, "retried_notifications": retried}

@router.get("/orders/debug/tiktok")
async def debug_tiktok_orders():
    """Return the raw TikTok order search API response for diagnosis."""
    if os.getenv("ENABLE_DEBUG_ENDPOINTS", "false").lower() != "true":
        raise HTTPException(404, "Not found")
    import json, time
    import httpx
    try:
        from app.scrapers.tiktok import TikTokScraper
    except Exception:
        raise _scrapers_disabled_error("TikTok debug orders")
    scraper = TikTokScraper()
    await scraper.start()
    shop_cipher = await scraper._get_shop_cipher()
    path = "/order/202309/orders/search"
    body = {
        "create_time_ge": int(time.time()) - 86400,
        "create_time_lt": int(time.time()),
    }
    body_str = json.dumps(body, separators=(",", ":"))
    params = scraper._base_params(path, {"shop_cipher": shop_cipher, "page_size": 50}, body_str)
    async with httpx.AsyncClient() as client:
        r = await client.post(
            scraper.BASE_URL + path,
            params=params,
            headers=scraper._headers(),
            content=body_str,
            timeout=30,
        )
    return {"http_status": r.status_code, "body": r.json() if r.text else None}

@router.get("/orders")
def list_orders(
    platform: Optional[Literal["shopee", "shopee_sg", "lazada", "tiktok"]] = None,
    limit: int = Query(default=50, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """List stored orders, optionally filtered by platform."""
    q = db.query(Order).order_by(func.coalesce(Order.platform_created_at, Order.created_at).desc())
    if platform:
        q = q.filter(Order.platform == platform)
    orders = q.limit(limit).all()
    return [
        {
            "id": o.id,
            "platform": o.platform,
            "platform_order_id": o.platform_order_id,
            "status": o.status,
            "buyer_name": o.buyer_name,
            "total_price": o.total_price,
            "items_count": o.items_count,
            "notified": o.notified,
            "platform_created_at": o.platform_created_at,
            "items": [
                {"sku": i.platform_sku, "name": i.product_name, "qty": i.quantity, "unit_price": i.unit_price}
                for i in o.items
            ],
        }
        for o in orders
    ]


@router.post("/notifications/test/{platform}")
async def test_notification_platform(platform: str):
    """Send a test ntfy notification without requiring a real order."""
    platform = (platform or "").strip().lower()
    if platform not in ("shopee", "lazada", "tiktok"):
        raise HTTPException(400, "platform must be one of: shopee, lazada, tiktok")
    out = await _send_test_notification(platform)
    if not out["ok"]:
        raise HTTPException(502, f"Test notification failed for {platform}")
    return {"status": "ok", **out}


@router.post("/notifications/test-all")
async def test_notification_all():
    """Send test ntfy notifications for Shopee, Lazada and TikTok."""
    results = []
    for platform in ("shopee", "lazada", "tiktok"):
        try:
            results.append(await _send_test_notification(platform))
        except Exception as e:
            results.append({"platform": platform, "ok": False, "error": str(e)})
    return {
        "status": "ok",
        "results": results,
    }

@router.get("/orders/summary")
def order_summary(db: Session = Depends(get_db)):
    orders = db.query(Order).all()
    total = len(orders)
    notified = sum(1 for o in orders if o.notified)
    pending = total - notified

    by_platform = {}
    for p in ("shopee", "lazada", "tiktok"):
        rows = [o for o in orders if o.platform == p]
        n_total = len(rows)
        n_notified = sum(1 for o in rows if o.notified)
        by_platform[p] = {
            "total": n_total,
            "notified": n_notified,
            "pending": n_total - n_notified,
        }

    return {
        "total": total,
        "notified": notified,
        "pending": pending,
        "by_platform": by_platform,
    }

@router.post("/orders/retry-notifications")
async def retry_order_notifications(limit: int = Query(default=200, ge=1, le=1000), db: Session = Depends(get_db)):
    retried = await order_engine.retry_missed_notifications(db, limit=min(limit, 1000))
    return {"status": "ok", "retried_notifications": retried}

@router.post("/orders/{order_id}/notify")
async def resend_order_notification(order_id: int, db: Session = Depends(get_db)):
    ok = await order_engine.notify_order_by_id(db, order_id)
    if not ok:
        raise HTTPException(404, "Order not found or notification failed")
    return {"status": "ok", "order_id": order_id}


def _get_shopee_order_or_404(order_key: str, db: Session) -> Order:
    """Resolve Shopee order by internal numeric id OR platform order_sn."""
    order = None
    key = str(order_key or "").strip()

    if key.isdigit():
        order = db.query(Order).filter_by(id=int(key)).first()

    if not order:
        order = db.query(Order).filter_by(platform_order_id=key).first()

    if not order:
        raise HTTPException(404, "Order not found")
    if order.platform not in ("shopee", "shopee_sg"):
        raise HTTPException(400, f"Order {order_key} is not a Shopee order")
    return order


def _get_lazada_order_or_404(order_id: int, db: Session) -> Order:
    order = db.query(Order).filter_by(id=order_id).first()
    if not order:
        raise HTTPException(404, "Order not found")
    if order.platform != "lazada":
        raise HTTPException(400, f"Order {order_id} is not a Lazada order")
    return order


def _get_tiktok_order_or_404(order_id: int, db: Session) -> Order:
    order = db.query(Order).filter_by(id=order_id).first()
    if not order:
        raise HTTPException(404, "Order not found")
    if order.platform != "tiktok":
        raise HTTPException(400, f"Order {order_id} is not a TikTok order")
    return order


@router.post("/orders/shopee/{order_id}/arrange-shipment")
def shopee_arrange_shipment_order(order_id: str, body: ShopeeShipmentRequest, db: Session = Depends(get_db)):
    """Arrange Shopee shipment for a stored order (ship_order)."""
    order = _get_shopee_order_or_404(order_id, db)
    try:
        out = shopee_arrange_shipment(
            platform=order.platform,
            order_sn=order.platform_order_id,
            package_number=(body.package_number or ""),
            pickup=(body.pickup or None),
            dropoff=(body.dropoff or None),
            non_integrated=(body.non_integrated or None),
        )
        return {
            "status": "ok",
            "order_id": order.id,
            "platform": order.platform,
            "platform_order_id": order.platform_order_id,
            "response": out,
        }
    except Exception as e:
        raise HTTPException(400, "Shopee arrange shipment failed")


@router.get("/orders/shopee/{order_id}/shipping-parameter")
def shopee_get_shipping_parameter_order(order_id: str, db: Session = Depends(get_db)):
    """Fetch Shopee shipping parameter as mandatory preflight before ship_order."""
    order = _get_shopee_order_or_404(order_id, db)
    try:
        out = shopee_get_shipping_parameter(
            platform=order.platform,
            order_sn=order.platform_order_id,
        )
        return {
            "status": "ok",
            "order_id": order.id,
            "platform": order.platform,
            "platform_order_id": order.platform_order_id,
            **out,
        }
    except Exception as e:
        raise HTTPException(400, "Shopee shipping-parameter preflight failed")


@router.post("/orders/shopee/{order_id}/awb/create")
def shopee_create_awb_order(order_id: str, body: ShopeeAwbRequest, db: Session = Depends(get_db)):
    """
    Create Shopee shipping document (AWB/label) and return latest result.
    AWB creation is allowed when shipping_carrier or tracking_number is present, regardless of order status.
    This matches confirmed Shopee SPX behavior: AWB is printable after shipment is processed, not just at READY_TO_SHIP.
    """
    order = _get_shopee_order_or_404(order_id, db)
    logger.info(
        "[Shopee AWB][create][start] order_id=%s platform=%s order_sn=%s package_number=%s shipping_document_type=%s wait_seconds=%s poll_seconds=%s",
        order.id,
        order.platform,
        order.platform_order_id,
        (body.package_number or ""),
        (body.shipping_document_type or ""),
        max(0, int(body.wait_seconds or 0)),
        max(1, int(body.poll_seconds or 1)),
    )
    try:
        out = shopee_create_awb(
            platform=order.platform,
            order_sn=order.platform_order_id,
            package_number=(body.package_number or ""),
            shipping_document_type=(body.shipping_document_type or ""),
            wait_seconds=max(0, int(body.wait_seconds or 0)),
            poll_seconds=max(1, int(body.poll_seconds or 1)),
        )
        logger.info(
            "[Shopee AWB][create][ok] order_id=%s order_sn=%s package_number=%s booking_no=%s print_url=%s warning=%s",
            order.id,
            order.platform_order_id,
            out.get("package_number", ""),
            out.get("booking_no", ""),
            bool(out.get("print_url")),
            out.get("warning", ""),
        )
        return {
            "status": "ok",
            "order_id": order.id,
            "platform": order.platform,
            "platform_order_id": order.platform_order_id,
            **out,
        }
    except Exception as e:
        logger.exception(
            "[Shopee AWB][create][fail] order_id=%s platform=%s order_sn=%s package_number=%s",
            order.id,
            order.platform,
            order.platform_order_id,
            (body.package_number or ""),
        )
        raise HTTPException(400, f"Shopee AWB create failed: {e}")


@router.get("/orders/shopee/{order_id}/awb")
def shopee_get_awb_order(order_id: str, package_number: str = "", db: Session = Depends(get_db)):
    """Fetch current Shopee AWB generation result (document URL/status) for a stored order."""
    order = _get_shopee_order_or_404(order_id, db)
    logger.info(
        "[Shopee AWB][get][start] order_id=%s platform=%s order_sn=%s package_number=%s",
        order.id,
        order.platform,
        order.platform_order_id,
        (package_number or ""),
    )
    try:
        out = shopee_get_awb_result(
            platform=order.platform,
            order_sn=order.platform_order_id,
            package_number=package_number,
        )
        logger.info(
            "[Shopee AWB][get][ok] order_id=%s order_sn=%s package_number=%s result_status=%s print_url=%s",
            order.id,
            order.platform_order_id,
            out.get("package_number", ""),
            out.get("result_status", ""),
            bool(out.get("print_url")),
        )
        return {
            "status": "ok",
            "order_id": order.id,
            "platform": order.platform,
            "platform_order_id": order.platform_order_id,
            **out,
        }
    except Exception as e:
        logger.exception(
            "[Shopee AWB][get][fail] order_id=%s platform=%s order_sn=%s package_number=%s",
            order.id,
            order.platform,
            order.platform_order_id,
            (package_number or ""),
        )
        raise HTTPException(400, f"Shopee AWB result fetch failed: {e}")


@router.get("/orders/shopee/{order_id}/awb/parameter")
def shopee_get_awb_parameter_order(order_id: str, package_number: str = "", db: Session = Depends(get_db)):
    """Fetch Shopee shipping document parameter for a stored order."""
    order = _get_shopee_order_or_404(order_id, db)
    try:
        out = shopee_get_awb_parameter(
            platform=order.platform,
            order_sn=order.platform_order_id,
            package_number=package_number,
        )
        return {
            "status": "ok",
            "order_id": order.id,
            "platform": order.platform,
            "platform_order_id": order.platform_order_id,
            **out,
        }
    except Exception as e:
        raise HTTPException(400, "Shopee AWB parameter fetch failed")


@router.get("/orders/shopee/{order_id}/tracking")
def shopee_get_tracking_order(order_id: str, package_number: str = "", db: Session = Depends(get_db)):
    """Fetch Shopee tracking-number info for a stored order."""
    order = _get_shopee_order_or_404(order_id, db)
    try:
        out = shopee_get_tracking_number(
            platform=order.platform,
            order_sn=order.platform_order_id,
            package_number=package_number,
        )
        return {
            "status": "ok",
            "order_id": order.id,
            "platform": order.platform,
            "platform_order_id": order.platform_order_id,
            **out,
        }
    except Exception as e:
        raise HTTPException(400, "Shopee tracking fetch failed")


@router.post("/orders/lazada/{order_id}/arrange-shipment")
async def lazada_arrange_shipment_order(order_id: int, body: LazadaShipmentRequest, db: Session = Depends(get_db)):
    """Arrange Lazada shipment for a stored order."""
    order = _get_lazada_order_or_404(order_id, db)
    try:
        out = await lazada_arrange_shipment(
            order.platform_order_id,
            delivery_type=(body.delivery_type or "").strip(),
            shipping_provider=(body.shipping_provider or "").strip(),
            tracking_number=(body.tracking_number or "").strip(),
            order_item_ids=body.order_item_ids,
        )
        return {
            "status": "ok",
            "order_id": order.id,
            "platform": order.platform,
            "platform_order_id": order.platform_order_id,
            **out,
        }
    except Exception as e:
        raise HTTPException(400, "Lazada arrange shipment failed")


@router.post("/orders/lazada/{order_id}/awb/create")
async def lazada_create_awb_order(order_id: int, body: LazadaAwbRequest, db: Session = Depends(get_db)):
    """Create Lazada AWB and optionally poll for printable URL."""
    order = _get_lazada_order_or_404(order_id, db)
    try:
        out = await lazada_create_awb(
            order.platform_order_id,
            wait_seconds=max(0, int(body.wait_seconds or 0)),
            poll_seconds=max(1, int(body.poll_seconds or 1)),
        )
        return {
            "status": "ok",
            "order_id": order.id,
            "platform": order.platform,
            "platform_order_id": order.platform_order_id,
            **out,
        }
    except Exception as e:
        raise HTTPException(400, "Lazada AWB create failed")


@router.get("/orders/lazada/{order_id}/awb")
async def lazada_get_awb_order(order_id: int, db: Session = Depends(get_db)):
    """Fetch Lazada AWB result and normalized print URL."""
    order = _get_lazada_order_or_404(order_id, db)
    try:
        out = await lazada_get_awb_result(order.platform_order_id)
        return {
            "status": "ok",
            "order_id": order.id,
            "platform": order.platform,
            "platform_order_id": order.platform_order_id,
            **out,
        }
    except Exception as e:
        raise HTTPException(400, "Lazada AWB result fetch failed")


@router.post("/orders/tiktok/{order_id}/arrange-shipment")
async def tiktok_arrange_shipment_order(order_id: int, body: TikTokShipmentRequest, db: Session = Depends(get_db)):
    """Arrange TikTok shipment for a stored order."""
    order = _get_tiktok_order_or_404(order_id, db)
    logger.info(
        "[TikTok AWB][arrange][start] order_id=%s platform=%s order_sn=%s package_id=%s",
        order.id,
        order.platform,
        order.platform_order_id,
        (body.package_id or ""),
    )
    try:
        out = await tiktok_arrange_shipment(
            order.platform_order_id,
            package_id=(body.package_id or ""),
        )
        logger.info(
            "[TikTok AWB][arrange][ok] order_id=%s order_sn=%s package_id=%s used_path=%s",
            order.id,
            order.platform_order_id,
            out.get("package_id", ""),
            out.get("used_path", ""),
        )
        return {
            "status": "ok",
            "order_id": order.id,
            "platform": order.platform,
            "platform_order_id": order.platform_order_id,
            **out,
        }
    except Exception as e:
        logger.exception(
            "[TikTok AWB][arrange][fail] order_id=%s platform=%s order_sn=%s package_id=%s",
            order.id,
            order.platform,
            order.platform_order_id,
            (body.package_id or ""),
        )
        raise HTTPException(400, f"TikTok arrange shipment failed: {e}")


@router.post("/orders/tiktok/{order_id}/awb/create")
async def tiktok_create_awb_order(order_id: int, body: TikTokAwbRequest, db: Session = Depends(get_db)):
    """Create TikTok AWB and optionally poll for printable URL."""
    order = _get_tiktok_order_or_404(order_id, db)
    logger.info(
        "[TikTok AWB][create][start] order_id=%s platform=%s order_sn=%s package_id=%s wait_seconds=%s poll_seconds=%s",
        order.id,
        order.platform,
        order.platform_order_id,
        (body.package_id or ""),
        max(0, int(body.wait_seconds or 0)),
        max(1, int(body.poll_seconds or 1)),
    )
    try:
        out = await tiktok_create_awb(
            order.platform_order_id,
            package_id=(body.package_id or ""),
            wait_seconds=max(0, int(body.wait_seconds or 0)),
            poll_seconds=max(1, int(body.poll_seconds or 1)),
        )
        logger.info(
            "[TikTok AWB][create][ok] order_id=%s order_sn=%s package_id=%s used_path=%s print_url=%s",
            order.id,
            order.platform_order_id,
            out.get("package_id", ""),
            out.get("used_path", ""),
            bool(out.get("print_url", "")),
        )
        return {
            "status": "ok",
            "order_id": order.id,
            "platform": order.platform,
            "platform_order_id": order.platform_order_id,
            **out,
        }
    except Exception as e:
        logger.exception(
            "[TikTok AWB][create][fail] order_id=%s platform=%s order_sn=%s package_id=%s",
            order.id,
            order.platform,
            order.platform_order_id,
            (body.package_id or ""),
        )
        raise HTTPException(400, f"TikTok AWB create failed: {e}")


@router.get("/orders/tiktok/{order_id}/awb")
async def tiktok_get_awb_order(order_id: int, package_id: str = "", db: Session = Depends(get_db)):
    """Fetch TikTok AWB result and normalized print URL."""
    order = _get_tiktok_order_or_404(order_id, db)
    logger.info(
        "[TikTok AWB][get][start] order_id=%s platform=%s order_sn=%s package_id=%s",
        order.id,
        order.platform,
        order.platform_order_id,
        (package_id or ""),
    )
    try:
        out = await tiktok_get_awb_result(order.platform_order_id, package_id=package_id)
        logger.info(
            "[TikTok AWB][get][ok] order_id=%s order_sn=%s package_id=%s used_path=%s print_url=%s",
            order.id,
            order.platform_order_id,
            out.get("package_id", ""),
            out.get("used_path", ""),
            bool(out.get("print_url", "")),
        )
        return {
            "status": "ok",
            "order_id": order.id,
            "platform": order.platform,
            "platform_order_id": order.platform_order_id,
            **out,
        }
    except TikTokAwbStateConflictError as e:
        logger.warning(
            "[TikTok AWB][get][conflict] order_id=%s platform=%s order_sn=%s package_id=%s code=%s",
            order.id,
            order.platform,
            order.platform_order_id,
            (package_id or ""),
            e.code,
        )
        raise HTTPException(409, f"TikTok AWB conflict (code={e.code}): {e.message}")
    except Exception as e:
        logger.exception(
            "[TikTok AWB][get][fail] order_id=%s platform=%s order_sn=%s package_id=%s",
            order.id,
            order.platform,
            order.platform_order_id,
            (package_id or ""),
        )
        raise HTTPException(400, f"TikTok AWB result fetch failed: {e}")

# ── Image (product level) ─────────────────────────────────────────────────────
@router.post("/products/{product_id}/image")
def upload_product_image(product_id: int, body: ImageUploadRequest, db: Session = Depends(get_db)):
    p = db.query(Product).filter_by(id=product_id).first()
    if not p: raise HTTPException(404, "Product not found")
    try:
        header, data = body.image_data.split(",", 1) if "," in body.image_data else ("", body.image_data)
        if header and not header.lower().startswith("data:image/"):
            raise HTTPException(400, "Invalid image payload")
        ext = "png" if "png" in header else "webp" if "webp" in header else "jpg"
        try:
            img_bytes = base64.b64decode(data, validate=True)
        except binascii.Error:
            raise HTTPException(400, "Invalid base64 image payload")
        if len(img_bytes) > 5 * 1024 * 1024:
            raise HTTPException(400, "Image too large")
        img_path = IMAGES_DIR / f"product_{product_id}.{ext}"
        img_path.write_bytes(img_bytes)
        p.image_url = f"/api/products/{product_id}/image/file"
        db.commit()
        return {"status": "ok", "image_url": p.image_url}
    except Exception as e:
        raise HTTPException(400, "Image upload failed")

@router.get("/products/{product_id}/image/file")
def get_product_image(product_id: int):
    for ext in ["jpg", "png", "webp"]:
        path = IMAGES_DIR / f"product_{product_id}.{ext}"
        if path.exists():
            return FileResponse(str(path))
    raise HTTPException(404, "No image found")


@router.get("/documents/image/{filename}")
def get_document_image(filename: str):
    allowed_names = {"header.png", "chop_and_sign.png"}
    safe_name = Path(filename).name
    if safe_name not in allowed_names:
        raise HTTPException(404, "No image found")

    path = IMAGES_DIR / safe_name
    if path.exists():
        return FileResponse(str(path))
    raise HTTPException(404, "No image found")