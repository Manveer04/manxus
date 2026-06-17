"""
Financial API routes — mount this in your main app.

In app/main.py (or wherever you create the FastAPI app), add:
    from app.financials.routes import router as financials_router
    app.include_router(financials_router, prefix="/api/financials", tags=["financials"])
"""
import asyncio
import json
import os
import re
import tempfile
from decimal import Decimal, InvalidOperation
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.marketplace import marketplace_unavailable
from app.financials.models import (
    FinancialTransaction,
    ProductCost,
    PurchaseBatch,
    OffPlatformSale,
    OffPlatformSaleItem,
    OffPlatformBuyerContact,
    SupplierContact,
    supplier_products,
    GeneratedInvoice,
    InvoiceLineItem,
    GeneratedPurchaseOrder,
    PurchaseOrderLineItem,
    PackagingSupplierCategory,
    PackagingSupplierContact,
    PackagingPurchase,
    packaging_supplier_category_links,
    packaging_purchase_category_links,
)
from app.financials.parsers import detect_and_parse

# Photos stored here (inside Docker volume so they survive restarts)
PHOTOS_DIR = Path("/app/data/purchases/photos")
PHOTOS_DIR.mkdir(parents=True, exist_ok=True)

router = APIRouter()

ALLOWED_PLATFORMS = {"shopee", "lazada", "tiktok", "shopee_sg"}


def _validate_platform_or_400(platform: Optional[str]) -> Optional[str]:
    if platform is None:
        return None
    p = platform.strip().lower()
    if p not in ALLOWED_PLATFORMS:
        raise HTTPException(400, "Invalid platform")
    return p


def _validate_month_or_400(month: Optional[str]) -> Optional[str]:
    if month is None:
        return None
    m = month.strip()
    if not re.match(r"^\d{4}-\d{2}$", m):
        raise HTTPException(400, "month must be YYYY-MM")
    return m


def _enforce_max_decimal_places(value: float, field_name: str, max_places: int = 4) -> None:
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise HTTPException(400, f"{field_name} must be a valid number")

    places = max(0, -dec.as_tuple().exponent)
    if places > max_places:
        raise HTTPException(400, f"{field_name} supports up to {max_places} decimal places")


def _run_async(coro):
    """Run coroutine from sync code safely (including worker threads)."""
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    if loop.is_running():
        tmp = asyncio.new_event_loop()
        try:
            return tmp.run_until_complete(coro)
        finally:
            tmp.close()

    return loop.run_until_complete(coro)


def _safe_json_loads(raw: Optional[str]):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        # Keep API response resilient even if legacy rows stored non-JSON text.
        return {"raw": str(raw)}


def _parse_json_string_list(raw: Optional[str], field_name: str) -> list[str]:
    if not raw:
        return []
    try:
        values = json.loads(raw)
    except Exception:
        raise HTTPException(400, f"{field_name} must be a JSON array of strings")
    if not isinstance(values, list):
        raise HTTPException(400, f"{field_name} must be a JSON array of strings")

    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise HTTPException(400, f"{field_name} must contain only strings")
        item = value.strip()
        if not item:
            continue
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _get_or_create_packaging_category(db: Session, name: str) -> PackagingSupplierCategory:
    category_name = (name or "").strip()
    if not category_name:
        raise HTTPException(400, "Category name is required")

    existing = (
        db.query(PackagingSupplierCategory)
        .filter(func.lower(PackagingSupplierCategory.name) == category_name.lower())
        .first()
    )
    if existing:
        if existing.name != category_name:
            existing.name = category_name
        return existing

    category = PackagingSupplierCategory(name=category_name)
    db.add(category)
    return category


def _packaging_supplier_category_names(supplier: PackagingSupplierContact) -> list[str]:
    return sorted(
        [c.name for c in (supplier.categories or []) if c and c.name],
        key=lambda value: value.lower(),
    )


def _packaging_supplier_to_dict(supplier: PackagingSupplierContact) -> dict:
    category_names = _packaging_supplier_category_names(supplier)
    return {
        "id": supplier.id,
        "name": supplier.name,
        "contact": supplier.contact,
        "company_name": supplier.company_name,
        "phone_country_code": supplier.phone_country_code or "+60",
        "phone_number": supplier.phone_number,
        "address": supplier.address,
        "categories": category_names,
        "category_count": len(category_names),
        "created_at": str(supplier.created_at),
        "updated_at": str(supplier.updated_at),
    }


def _packaging_purchase_to_dict(purchase: PackagingPurchase) -> dict:
    category_names = sorted(
        [c.name for c in (purchase.categories or []) if c and c.name],
        key=lambda value: value.lower(),
    )
    return {
        "id": purchase.id,
        "packaging_supplier_id": purchase.packaging_supplier_id,
        "supplier_name": purchase.supplier_name,
        "purchase_date": str(purchase.purchase_date),
        "qty": purchase.qty,
        "unit_cost": purchase.unit_cost,
        "total_cost": purchase.total_cost,
        "product_name": purchase.product_name,
        "notes": purchase.notes,
        "photo_item": purchase.photo_item,
        "photo_receipt": purchase.photo_receipt,
        "categories": category_names,
        "category_count": len(category_names),
        "created_at": str(purchase.created_at),
    }


# ── DB dependency ──────────────────────────────────────────────────────────────
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _enrich_tiktok(db, txn_rows):
    """
    For TikTok transactions, resolve product_id stored in sku → seller SKU + product name
    via the PlatformListing table, which the inventory sync populates.
    For rows where Shopping center items was '/' (sku=None), calls the TikTok Order API.
    """
    from app.models import PlatformListing

    # Lazily create scraper only if needed (for API fallback)
    _scraper = None
    def _get_scraper():
        nonlocal _scraper
        if _scraper is None:
            try:
                raise marketplace_unavailable("TikTok transaction enrichment", "tiktok")
            except Exception:
                _scraper = None
        return _scraper

    enriched = 0
    for txn in txn_rows:
        if txn.platform != "tiktok":
            continue
        # sku holds the TikTok SKU id (numeric string) set by the parser
        if txn.sku and txn.sku.isdigit():
            listing = db.query(PlatformListing).filter_by(
                platform="tiktok", platform_sku=txn.sku
            ).first()
            if listing:
                if listing.platform_sku:
                    txn.sku = listing.platform_sku
                if not txn.product_name and listing.product:
                    txn.product_name = listing.product.name
                    enriched += 1
            # If listing missing or has no linked product, fall back to Order API
            if not txn.product_name:
                scraper = _get_scraper()
                if not scraper:
                    continue
                info = scraper.get_order_sku_sync(txn.order_id)
                if info.get("product_name"):
                    txn.product_name = info["product_name"]
                    enriched += 1
                if info.get("sku") and not txn.sku:
                    txn.sku = info["sku"]
        elif not txn.sku:
            # Shopping center items was '/' — call TikTok Order API to get line items
            scraper = _get_scraper()
            if not scraper:
                continue
            info = scraper.get_order_sku_sync(txn.order_id)
            if info.get("sku"):
                txn.sku = info["sku"]
                if not txn.product_name and info.get("product_name"):
                    txn.product_name = info["product_name"]
                if info.get("qty") and (txn.qty or 1) == 1:
                    txn.qty = info["qty"]
                # Now resolve via PlatformListing for canonical product name
                listing = db.query(PlatformListing).filter_by(
                    platform="tiktok", platform_sku=txn.sku
                ).first()
                if listing and not txn.product_name and listing.product:
                    txn.product_name = listing.product.name
                enriched += 1
        if not txn.sku and not txn.product_name:
            # Last resort — try Orders table for older synced records
            from app.models import Order
            order = db.query(Order).filter_by(
                platform="tiktok", platform_order_id=txn.order_id
            ).first()
            if order and order.items:
                item = order.items[0]
                if item.product_name:
                    txn.product_name = item.product_name
                if item.platform_sku:
                    txn.sku = item.platform_sku
                if item.quantity and (txn.qty or 1) == 1:
                    txn.qty = item.quantity
                enriched += 1
    return enriched


def _enrich_lazada(db, txn_rows):
    """
    For Lazada transactions, call the order items API to get the real quantity.
    The income report has one row per order (qty defaults to 1).
    The Lazada items API returns one row *per unit sold*, so counting rows = real qty.
    Also fills in product_name and sku if missing.
    """
    raise marketplace_unavailable("Lazada quantity enrichment", "lazada")


def _enrich_shopee(db, txn_rows):
    """
    Re-fetch actual item quantities from the Shopee Orders API for already-stored rows.
    Uses the same _shopee_fetch_order_qtys helper from parsers.py.
    """
    from app.financials.parsers import _shopee_fetch_order_qtys, _normalize_order_id

    order_ids = [
        _normalize_order_id(t.order_id)
        for t in txn_rows
        if t.platform in ("shopee", "shopee_sg") and t.order_id
    ]
    order_ids = [oid for oid in order_ids if oid]
    if not order_ids:
        return 0
    print(f"[Enrich Shopee] Fetching quantities for {len(order_ids)} orders...")
    qtys = _shopee_fetch_order_qtys(order_ids)
    enriched = 0
    resolved = 0
    for txn in txn_rows:
        if txn.platform not in ("shopee", "shopee_sg"):
            continue
        normalized = _normalize_order_id(txn.order_id)
        new_qty = qtys.get(normalized)
        if new_qty:
            resolved += 1
        if new_qty and new_qty != (txn.qty or 1):
            txn.qty = new_qty
            enriched += 1
    print(
        f"[Enrich Shopee] Done: enriched={enriched} resolved={resolved} total={len(order_ids)}"
    )
    return enriched


# ── Upload endpoint ────────────────────────────────────────────────────────────

@router.post("/upload")
async def upload_financial_report(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """
    Accept an Excel file from Shopee / Lazada / TikTok, parse it,
    and upsert all transactions into the DB.
    Returns a summary of what was inserted/updated.
    """
    if not file.filename.endswith((".xlsx", ".xls", ".csv")):
        raise HTTPException(400, "Only .xlsx / .xls / .csv files are supported")

    # Save to temp file
    suffix = os.path.splitext(file.filename)[1]
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    try:
        transactions = detect_and_parse(tmp_path, upload_batch=file.filename)
    except Exception as e:
        raise HTTPException(400, "Unable to parse report file")
    finally:
        os.unlink(tmp_path)

    # Filter out any rows with empty order_id just in case
    transactions = [t for t in transactions if t.get("order_id", "").strip()]

    inserted = 0
    updated  = 0
    skipped  = 0

    for t in transactions:
        try:
            existing = (
                db.query(FinancialTransaction)
                .filter_by(platform=t["platform"], order_id=t["order_id"])
                .first()
            )
            if existing:
                for k, v in t.items():
                    setattr(existing, k, v)
                updated += 1
            else:
                db.add(FinancialTransaction(**t))
                inserted += 1
            db.flush()
        except Exception:
            db.rollback()
            skipped += 1
            continue

    db.commit()

    # Enrich TikTok rows with product info from orders table
    if transactions and transactions[0].get("platform") == "tiktok":
        tiktok_rows = db.query(FinancialTransaction).filter(
            FinancialTransaction.platform == "tiktok",
            FinancialTransaction.upload_batch == file.filename,
        ).all()
        enriched = _enrich_tiktok(db, tiktok_rows)
        db.commit()
    elif transactions and transactions[0].get("platform") == "lazada":
        lazada_rows = db.query(FinancialTransaction).filter(
            FinancialTransaction.platform == "lazada",
            FinancialTransaction.upload_batch == file.filename,
        ).all()
        enriched = _enrich_lazada(db, lazada_rows)
        db.commit()
    else:
        enriched = 0

    platform = transactions[0]["platform"] if transactions else "unknown"
    months   = sorted({t["month"] for t in transactions})

    return {
        "status":    "ok",
        "platform":  platform,
        "filename":  file.filename,
        "inserted":  inserted,
        "updated":   updated,
        "skipped":   skipped,
        "total":     len(transactions),
        "months":    months,
    }


# ── Backfill TikTok product info ──────────────────────────────────────────────

# ── Backfill Lazada quantity ───────────────────────────────────────────────────

@router.post("/backfill-lazada")
def backfill_lazada(db: Session = Depends(get_db)):
    """Re-enrich all Lazada transactions to populate real quantity from the order items API."""
    rows = db.query(FinancialTransaction).filter(
        FinancialTransaction.platform == "lazada",
    ).all()
    enriched = _enrich_lazada(db, rows)
    db.commit()
    return {"status": "ok", "enriched": enriched, "total": len(rows)}


@router.post("/backfill-shopee")
def backfill_shopee(db: Session = Depends(get_db)):
    """Re-fetch real item quantities from Shopee Orders API for all stored Shopee transactions."""
    rows = db.query(FinancialTransaction).filter(
        FinancialTransaction.platform == "shopee",
    ).all()
    enriched = _enrich_shopee(db, rows)
    db.commit()
    return {"status": "ok", "enriched": enriched, "total": len(rows)}


@router.post("/backfill-tiktok")
def backfill_tiktok(db: Session = Depends(get_db)):
    """Re-enrich all TikTok transactions: resolve product name + SKU via Order API."""
    rows = db.query(FinancialTransaction).filter(
        FinancialTransaction.platform == "tiktok",
    ).all()
    enriched = _enrich_tiktok(db, rows)
    db.commit()
    return {"status": "ok", "enriched": enriched, "total": len(rows)}


# ── P&L summary endpoint ───────────────────────────────────────────────────────

@router.get("/summary")
def get_summary(
    year:     Optional[int] = None,
    month:    Optional[str] = None,   # YYYY-MM
    platform: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """
    Returns monthly P&L data, optionally filtered.
    Response shape:
    {
      "by_month": [ { month, platform, gross_revenue, total_fees, net_settlement, orders } ],
      "totals":   { gross_revenue, total_fees, net_settlement, orders },
      "platforms": ["shopee", "lazada", "tiktok"],
      "months":   ["2026-01", "2026-02", ...]
    }
    """
    month = _validate_month_or_400(month)
    platform = _validate_platform_or_400(platform)

    q = db.query(FinancialTransaction)
    if year:
        q = q.filter(FinancialTransaction.year == year)
    if month:
        q = q.filter(FinancialTransaction.month == month)
    if platform:
        q = q.filter(FinancialTransaction.platform == platform)

    rows = q.all()

    # FIFO-aware cost of goods sold
    fifo_cogs = _compute_fifo_cogs(db, rows)

    # Group by month + platform
    groups: dict = defaultdict(lambda: {
        "gross_revenue": 0.0,
        "commission_fee": 0.0,
        "transaction_fee": 0.0,
        "service_fee": 0.0,
        "other_fees": 0.0,
        "total_fees": 0.0,
        "net_shipping": 0.0,
        "voucher_seller": 0.0,
        "net_settlement": 0.0,
        "cogs": 0.0,
        "orders": 0,
    })

    for r in rows:
        key = (r.month, r.platform)
        g = groups[key]
        g["gross_revenue"]   += r.gross_revenue or 0
        g["commission_fee"]  += r.commission_fee or 0
        g["transaction_fee"] += r.transaction_fee or 0
        g["service_fee"]     += r.service_fee or 0
        g["other_fees"]      += r.other_fees or 0
        g["total_fees"]      += r.total_fees or 0
        g["net_shipping"]    += r.net_shipping or 0
        g["voucher_seller"]  += r.voucher_seller or 0
        g["net_settlement"]  += r.net_settlement or 0
        g["cogs"]            += fifo_cogs.get(r.id, 0.0)
        g["orders"]          += 1

    by_month = []
    for (m, p), v in sorted(groups.items()):
        profit_margin = (
            round(v["net_settlement"] / v["gross_revenue"] * 100, 1)
            if v["gross_revenue"] > 0 else 0
        )
        profit       = round(v["net_settlement"] - v["cogs"], 2)
        true_margin  = round(profit / v["gross_revenue"] * 100, 1) if v["gross_revenue"] > 0 else 0
        by_month.append({
            "month":          m,
            "platform":       p,
            "gross_revenue":  round(v["gross_revenue"], 2),
            "commission_fee": round(v["commission_fee"], 2),
            "transaction_fee":round(v["transaction_fee"], 2),
            "service_fee":    round(v["service_fee"], 2),
            "other_fees":     round(v["other_fees"], 2),
            "total_fees":     round(v["total_fees"], 2),
            "net_shipping":   round(v["net_shipping"], 2),
            "voucher_seller": round(v["voucher_seller"], 2),
            "net_settlement": round(v["net_settlement"], 2),
            "cogs":           round(v["cogs"], 2),
            "profit":         profit,
            "profit_margin":  profit_margin,
            "true_margin":    true_margin,
            "orders":         v["orders"],
        })

    # Overall totals
    total_cogs = sum(fifo_cogs.get(r.id, 0.0) for r in rows)
    totals = {
        "gross_revenue":  round(sum(r.gross_revenue or 0 for r in rows), 2),
        "total_fees":     round(sum(r.total_fees or 0 for r in rows), 2),
        "net_settlement": round(sum(r.net_settlement or 0 for r in rows), 2),
        "cogs":           round(total_cogs, 2),
        "profit":         round(sum(r.net_settlement or 0 for r in rows) - total_cogs, 2),
        "orders":         len(rows),
    }
    if totals["gross_revenue"] > 0:
        totals["profit_margin"] = round(
            totals["net_settlement"] / totals["gross_revenue"] * 100, 1
        )
        totals["true_margin"] = round(
            totals["profit"] / totals["gross_revenue"] * 100, 1
        )
    else:
        totals["profit_margin"] = 0
        totals["true_margin"]   = 0

    return {
        "by_month":  by_month,
        "totals":    totals,
        "platforms": sorted({r.platform for r in rows}),
        "months":    sorted({r.month for r in rows}),
    }


# ── Per-order detail endpoint ──────────────────────────────────────────────────

@router.get("/transactions")
def get_transactions(
    month:    Optional[str] = None,
    platform: Optional[str] = None,
    limit:    int = Query(default=100, ge=1, le=1000),
    offset:   int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    month = _validate_month_or_400(month)
    platform = _validate_platform_or_400(platform)

    q = db.query(FinancialTransaction)
    if month:
        q = q.filter(FinancialTransaction.month == month)
    if platform:
        q = q.filter(FinancialTransaction.platform == platform)
    total = q.count()
    rows  = q.order_by(FinancialTransaction.settlement_date.desc()).offset(offset).limit(limit).all()

    # FIFO-aware cost lookup for this page of results
    fifo_cogs = _compute_fifo_cogs(db, rows)

    return {
        "total": total,
        "items": [
            {
                "id":             r.id,
                "platform":       r.platform,
                "order_id":       r.order_id,
                "product_name":   r.product_name,
                "sku":            r.sku,
                "qty":            r.qty,
                "order_date":     str(r.order_date) if r.order_date else None,
                "settlement_date":str(r.settlement_date) if r.settlement_date else None,
                "month":          r.month,
                "gross_revenue":  r.gross_revenue,
                "total_fees":     r.total_fees,
                "net_shipping":   r.net_shipping,
                "net_settlement": r.net_settlement,
                "cogs":           round(fifo_cogs.get(r.id, 0.0), 2),
                "profit":         round((r.net_settlement or 0) - fifo_cogs.get(r.id, 0.0), 2),
            }
            for r in rows
        ],
    }


# ── Available months ───────────────────────────────────────────────────────────

@router.get("/months")
def get_months(db: Session = Depends(get_db)):
    months = db.query(FinancialTransaction.month).distinct().all()
    return sorted([m[0] for m in months])


# ── Product cost CRUD ─────────────────────────────────────────────────────────

@router.get("/product-costs")
def get_product_costs(db: Session = Depends(get_db)):
    """Return one merged row per group + individual rows for ungrouped products."""
    from app.models import ProductGroup

    costs = db.query(ProductCost).all()
    cost_names = {c.product_name.strip().lower() for c in costs}

    # Build group lookup
    groups_map: dict = {}
    product_to_gid: dict = {}
    for g in db.query(ProductGroup).all():
        groups_map[g.id] = g
        for member in g.members:
            product_to_gid[member.id] = g.id

    # Bucket costs
    group_costs: dict = {}
    ungrouped_costs = []
    for c in costs:
        gid = product_to_gid.get(c.product_id) if c.product_id else None
        if gid:
            group_costs.setdefault(gid, []).append(c)
        else:
            ungrouped_costs.append(c)

    # Covered SKUs for uncosted detection
    covered_skus = set()
    for c in costs:
        for s in [c.shopee_sku, c.lazada_sku, c.tiktok_sku, c.master_sku]:
            if s: covered_skus.add(s.strip().lower())

    # Build set of all (platform, sku) pairs that appear in uploaded transactions
    txn_skus: set = set()   # (platform, sku_lower)
    txn_names: set = set()  # product_name_lower
    for row in db.query(
        FinancialTransaction.platform,
        FinancialTransaction.sku,
        FinancialTransaction.product_name,
    ).filter(FinancialTransaction.sku.isnot(None)).distinct().all():
        if row.sku:
            txn_skus.add((row.platform, row.sku.strip().lower()))
        if row.product_name:
            txn_names.add(row.product_name.strip().lower())

    def is_matched(cost_entries) -> bool:
        """Return True if any SKU in these cost rows appears in uploaded transactions."""
        for c in cost_entries:
            for platform, sku in [
                ("shopee",  c.shopee_sku),
                ("lazada",  c.lazada_sku),
                ("tiktok",  c.tiktok_sku),
            ]:
                if sku and (platform, sku.strip().lower()) in txn_skus:
                    return True
            if c.master_sku and c.master_sku.strip().lower() in {s for _, s in txn_skus}:
                return True
            if c.product_name.strip().lower() in txn_names:
                return True
        return False

    has_any_txns = bool(txn_skus or txn_names)

    result = []

    # One merged row per group
    for gid, gcosts in sorted(group_costs.items(), key=lambda x: groups_map[x[0]].display_name.lower()):
        g = groups_map[gid]
        shopee_skus = sorted({c.shopee_sku for c in gcosts if c.shopee_sku})
        lazada_skus = sorted({c.lazada_sku for c in gcosts if c.lazada_sku})
        tiktok_skus = sorted({c.tiktok_sku for c in gcosts if c.tiktok_sku})
        ref   = next((c for c in gcosts if (c.cost_price or 0) > 0), gcosts[0])
        total = (ref.cost_price or 0) + (ref.packaging_cost or 0) + (ref.inbound_cost or 0)
        result.append({
            "is_group":        True,
            "group_id":        gid,
            "group_name":      g.display_name,
            "cost_ids":        [c.id for c in gcosts],
            "product_name":    g.display_name,
            "master_sku":      None,
            "shopee_skus":     shopee_skus,
            "lazada_skus":     lazada_skus,
            "tiktok_skus":     tiktok_skus,
            "cost_price":      ref.cost_price or 0,
            "packaging_cost":  ref.packaging_cost or 0,
            "inbound_cost":    ref.inbound_cost or 0,
            "total_unit_cost": total,
            "notes":           ref.notes,
            "matched":         is_matched(gcosts) if has_any_txns else None,
        })

    # Individual ungrouped rows
    for c in sorted(ungrouped_costs, key=lambda x: x.product_name.lower()):
        total = (c.cost_price or 0) + (c.packaging_cost or 0) + (c.inbound_cost or 0)
        result.append({
            "is_group":        False,
            "group_id":        None,
            "group_name":      None,
            "cost_ids":        [c.id],
            "product_name":    c.product_name,
            "master_sku":      c.master_sku,
            "shopee_skus":     [c.shopee_sku] if c.shopee_sku else [],
            "lazada_skus":     [c.lazada_sku] if c.lazada_sku else [],
            "tiktok_skus":     [c.tiktok_sku] if c.tiktok_sku else [],
            "cost_price":      c.cost_price or 0,
            "packaging_cost":  c.packaging_cost or 0,
            "inbound_cost":    c.inbound_cost or 0,
            "total_unit_cost": total,
            "notes":           c.notes,
            "matched":         is_matched([c]) if has_any_txns else None,
        })

    # Uncosted: products in transactions without any cost entry
    uncosted = []
    seen_names: set = set()
    txns = db.query(
        FinancialTransaction.product_name,
        FinancialTransaction.sku,
        FinancialTransaction.platform,
    ).filter(
        FinancialTransaction.product_name.isnot(None)
    ).distinct().all()

    for t in txns:
        name = (t.product_name or "").strip()
        sku  = (t.sku or "").strip().lower()
        key  = name.lower()
        if sku and sku in covered_skus: continue
        if key in cost_names: continue
        if key in seen_names: continue
        seen_names.add(key)
        uncosted.append({
            "product_name": name,
            "sku":          (t.sku or "").strip(),
            "platform":     t.platform,
            "cost_price":   None,
        })

    return {"costs": result, "uncosted": uncosted}


@router.post("/product-costs")
def upsert_product_cost(body: dict, db: Session = Depends(get_db)):
    """Create or update a product cost entry."""
    if not body.get("product_name"):
        raise HTTPException(400, "product_name is required")
    if body.get("cost_price") is None:
        raise HTTPException(400, "cost_price is required")

    existing = None
    if body.get("id"):
        existing = db.query(ProductCost).filter_by(id=body["id"]).first()
    if not existing and body.get("product_id"):
        existing = db.query(ProductCost).filter_by(product_id=body["product_id"]).first()
    if not existing:
        existing = db.query(ProductCost).filter(
            ProductCost.product_name.ilike(body["product_name"].strip())
        ).first()

    def clean(v): return (v or "").strip() or None

    if existing:
        existing.product_name   = body["product_name"].strip()
        existing.master_sku     = clean(body.get("master_sku"))
        existing.shopee_sku     = clean(body.get("shopee_sku"))
        existing.lazada_sku     = clean(body.get("lazada_sku"))
        existing.tiktok_sku     = clean(body.get("tiktok_sku"))
        existing.cost_price     = float(body["cost_price"])
        existing.packaging_cost = float(body.get("packaging_cost") or 0)
        existing.inbound_cost   = float(body.get("inbound_cost") or 0)
        existing.notes          = body.get("notes")
    else:
        existing = ProductCost(
            product_id      = body.get("product_id"),
            product_name    = body["product_name"].strip(),
            master_sku      = clean(body.get("master_sku")),
            shopee_sku      = clean(body.get("shopee_sku")),
            lazada_sku      = clean(body.get("lazada_sku")),
            tiktok_sku      = clean(body.get("tiktok_sku")),
            cost_price      = float(body["cost_price"]),
            packaging_cost  = float(body.get("packaging_cost") or 0),
            inbound_cost    = float(body.get("inbound_cost") or 0),
            notes           = body.get("notes"),
        )
        db.add(existing)

    db.commit()
    db.refresh(existing)
    return {"status": "ok", "id": existing.id, "total_unit_cost": existing.total_unit_cost}


@router.post("/product-costs/sync-from-inventory")
def sync_costs_from_inventory(db: Session = Depends(get_db)):
    """
    Auto-populate ProductCost rows from the inventory Product + PlatformListing tables.
    Only creates rows for products not already in product_costs.
    Safe to run multiple times — existing costs are never overwritten.
    """
    # Import inventory models at call time to avoid circular imports
    from app.models import Product, PlatformListing

    products = db.query(Product).all()
    created  = 0
    skipped  = 0

    for p in products:
        # Skip if already has a cost entry
        existing = db.query(ProductCost).filter_by(product_id=p.id).first()
        if not existing:
            existing = db.query(ProductCost).filter(
                ProductCost.product_name.ilike(p.name.strip())
            ).first()

        # Build SKU map from platform listings
        sku_map = {"shopee": None, "lazada": None, "tiktok": None}
        for listing in p.listings:
            platform = listing.platform.lower()
            if platform in sku_map and listing.platform_sku:
                sku_map[platform] = listing.platform_sku.strip()

        if existing:
            # Update SKUs even if cost entry already exists — keeps them in sync
            existing.product_id  = p.id
            existing.master_sku  = p.master_sku
            existing.shopee_sku  = sku_map["shopee"]  or existing.shopee_sku
            existing.lazada_sku  = sku_map["lazada"]  or existing.lazada_sku
            existing.tiktok_sku  = sku_map["tiktok"]  or existing.tiktok_sku
            skipped += 1
        else:
            db.add(ProductCost(
                product_id      = p.id,
                product_name    = p.name.strip(),
                master_sku      = p.master_sku,
                shopee_sku      = sku_map["shopee"],
                lazada_sku      = sku_map["lazada"],
                tiktok_sku      = sku_map["tiktok"],
                cost_price      = 0.0,   # seller fills this in
                packaging_cost  = 0.0,
                inbound_cost    = 0.0,
            ))
            created += 1

    db.commit()
    return {
        "status":  "ok",
        "created": created,
        "updated": skipped,
        "message": f"Synced {created} new products, updated SKUs for {skipped} existing entries",
    }


@router.post("/product-costs/bulk")
def bulk_update_cost_field(body: dict, db: Session = Depends(get_db)):
    """Update one cost field across multiple ProductCost rows (used for group inline edits)."""
    cost_ids = body.get("cost_ids", [])
    field    = body.get("field")
    value    = body.get("value", 0.0)

    if field not in ("cost_price", "packaging_cost", "inbound_cost"):
        raise HTTPException(400, "Invalid field")

    for cid in cost_ids:
        c = db.query(ProductCost).filter_by(id=cid).first()
        if c:
            setattr(c, field, float(value))
    db.commit()
    return {"status": "ok"}


@router.delete("/product-costs/{cost_id}")
def delete_product_cost(cost_id: int, db: Session = Depends(get_db)):
    c = db.query(ProductCost).filter_by(id=cost_id).first()
    if not c:
        raise HTTPException(404, "Not found")
    db.delete(c)
    db.commit()
    return {"status": "ok"}


# ── Purchase batches (stock-in / goods received) ───────────────────────────────

def _batch_to_dict(b: PurchaseBatch) -> dict:
    return {
        "id":            b.id,
        "product_id":    b.product_id,
        "product_name":  b.product_name,
        "purchase_date": str(b.purchase_date),
        "qty":           b.qty,
        "qty_remaining": b.qty_remaining,
        "unit_cost":     b.unit_cost,
        "total_cost":    b.total_cost,
        "supplier_name": b.supplier_name,
        "supplier_contact_id": b.supplier_contact_id,
        "notes":         b.notes,
        "photo_goods":   b.photo_goods,
        "photo_receipt": b.photo_receipt,
        "stock_pushed":  b.stock_pushed,
        "push_results":  _safe_json_loads(b.push_results),
        "created_at":    str(b.created_at),
    }


def _off_sale_to_dict(s: OffPlatformSale, db: Session) -> dict:
    items = (
        db.query(OffPlatformSaleItem)
        .filter_by(sale_id=s.id)
        .order_by(OffPlatformSaleItem.id.asc())
        .all()
    )
    return {
        "id": s.id,
        "sale_date": str(s.sale_date),
        "sold_to": s.sold_to,
        "buyer_contact_id": s.buyer_contact_id,
        "total_amount": s.total_amount,
        "notes": s.notes,
        "photo": s.photo,
        "push_results": _safe_json_loads(s.push_results),
        "created_at": str(s.created_at),
        "items": [
            {
                "id": i.id,
                "product_id": i.product_id,
                "product_name": i.product_name,
                "qty": i.qty,
                "unit_price": i.unit_price,
                "line_total": i.line_total,
            }
            for i in items
        ],
    }


@router.get("/contacts/suppliers")
def list_supplier_contacts(db: Session = Depends(get_db)):
    from app.models import Product

    rows = db.query(SupplierContact).order_by(SupplierContact.name.asc()).all()
    out = []
    for s in rows:
        product_rows = (
            db.query(Product)
            .join(supplier_products, supplier_products.c.product_id == Product.id)
            .filter(supplier_products.c.supplier_contact_id == s.id)
            .order_by(Product.name.asc())
            .all()
        )
        out.append(
            {
                "id": s.id,
                "name": s.name,
                "company_name": s.company_name,
                "phone_country_code": s.phone_country_code or "+60",
                "phone_number": s.phone_number,
                "address": s.address,
                "supplied_product_ids": [p.id for p in product_rows],
                "supplied_products": [{"id": p.id, "name": p.name} for p in product_rows],
                "created_at": str(s.created_at),
                "updated_at": str(s.updated_at),
            }
        )
    return {"items": out}


@router.post("/contacts/suppliers")
def create_supplier_contact(
    name: str = Form(...),
    phone_country_code: Optional[str] = Form("+60"),
    company_name: Optional[str] = Form(None),
    phone_number: str = Form(...),
    address: Optional[str] = Form(None),
    supplied_product_ids_json: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    from app.models import Product

    if not name.strip():
        raise HTTPException(400, "name is required")
    if not phone_number.strip():
        raise HTTPException(400, "phone_number is required")

    s = SupplierContact(
        name=name.strip(),
        contact=name.strip(),
        company_name=(company_name or "").strip() or None,
        phone_country_code=(phone_country_code or "+60").strip() or "+60",
        phone_number=phone_number.strip(),
        address=(address or "").strip() or None,
    )
    db.add(s)
    db.flush()

    supplied_ids = []
    if supplied_product_ids_json:
        try:
            supplied_ids = [int(x) for x in json.loads(supplied_product_ids_json)]
        except Exception:
            raise HTTPException(400, "supplied_product_ids_json must be a JSON array of product IDs")

    if supplied_ids:
        products = db.query(Product).filter(Product.id.in_(supplied_ids)).all()
        if len(products) != len(set(supplied_ids)):
            raise HTTPException(400, "One or more supplied product IDs are invalid")
        for p in products:
            db.execute(
                supplier_products.insert().values(
                    supplier_contact_id=s.id,
                    product_id=p.id,
                )
            )

    db.commit()
    return {"status": "ok", "id": s.id}


@router.put("/contacts/suppliers/{supplier_id}")
def update_supplier_contact(
    supplier_id: int,
    name: str = Form(...),
    phone_country_code: Optional[str] = Form("+60"),
    company_name: Optional[str] = Form(None),
    phone_number: str = Form(...),
    address: Optional[str] = Form(None),
    supplied_product_ids_json: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    from app.models import Product

    if not name.strip():
        raise HTTPException(400, "name is required")
    if not phone_number.strip():
        raise HTTPException(400, "phone_number is required")

    s = db.query(SupplierContact).filter_by(id=supplier_id).first()
    if not s:
        raise HTTPException(404, "Supplier not found")

    s.name = name.strip()
    s.contact = name.strip()
    s.company_name = (company_name or "").strip() or None
    s.phone_country_code = (phone_country_code or "+60").strip() or "+60"
    s.phone_number = phone_number.strip()
    s.address = (address or "").strip() or None

    supplied_ids = []
    if supplied_product_ids_json:
        try:
            supplied_ids = [int(x) for x in json.loads(supplied_product_ids_json)]
        except Exception:
            raise HTTPException(400, "supplied_product_ids_json must be a JSON array of product IDs")

    db.execute(
        supplier_products.delete().where(
            supplier_products.c.supplier_contact_id == s.id
        )
    )
    if supplied_ids:
        products = db.query(Product).filter(Product.id.in_(supplied_ids)).all()
        if len(products) != len(set(supplied_ids)):
            raise HTTPException(400, "One or more supplied product IDs are invalid")
        for p in products:
            db.execute(
                supplier_products.insert().values(
                    supplier_contact_id=s.id,
                    product_id=p.id,
                )
            )

    db.commit()
    return {"status": "ok", "id": s.id}


@router.delete("/contacts/suppliers/{supplier_id}")
def delete_supplier_contact(supplier_id: int, db: Session = Depends(get_db)):
    s = db.query(SupplierContact).filter_by(id=supplier_id).first()
    if not s:
        raise HTTPException(404, "Supplier not found")
    db.execute(
        supplier_products.delete().where(
            supplier_products.c.supplier_contact_id == s.id
        )
    )
    db.delete(s)
    db.commit()
    return {"status": "ok"}


@router.get("/contacts/packaging-suppliers")
def list_packaging_suppliers(db: Session = Depends(get_db)):
    rows = db.query(PackagingSupplierContact).order_by(PackagingSupplierContact.name.asc()).all()
    return {"items": [_packaging_supplier_to_dict(row) for row in rows]}


@router.post("/contacts/packaging-suppliers")
def create_packaging_supplier_contact(
    name: str = Form(...),
    phone_country_code: Optional[str] = Form("+60"),
    company_name: Optional[str] = Form(None),
    phone_number: str = Form(...),
    address: Optional[str] = Form(None),
    category_names_json: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    if not name.strip():
        raise HTTPException(400, "name is required")
    if not phone_number.strip():
        raise HTTPException(400, "phone_number is required")

    category_names = _parse_json_string_list(category_names_json, "category_names_json")

    supplier = PackagingSupplierContact(
        name=name.strip(),
        contact=name.strip(),
        company_name=(company_name or "").strip() or None,
        phone_country_code=(phone_country_code or "+60").strip() or "+60",
        phone_number=phone_number.strip(),
        address=(address or "").strip() or None,
    )
    db.add(supplier)
    db.flush()

    if category_names:
        categories = [_get_or_create_packaging_category(db, value) for value in category_names]
        supplier.categories = categories

    db.commit()
    return {"status": "ok", "id": supplier.id}


@router.put("/contacts/packaging-suppliers/{supplier_id}")
def update_packaging_supplier_contact(
    supplier_id: int,
    name: str = Form(...),
    phone_country_code: Optional[str] = Form("+60"),
    company_name: Optional[str] = Form(None),
    phone_number: str = Form(...),
    address: Optional[str] = Form(None),
    category_names_json: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    if not name.strip():
        raise HTTPException(400, "name is required")
    if not phone_number.strip():
        raise HTTPException(400, "phone_number is required")

    supplier = db.query(PackagingSupplierContact).filter_by(id=supplier_id).first()
    if not supplier:
        raise HTTPException(404, "Packaging supplier not found")

    category_names = _parse_json_string_list(category_names_json, "category_names_json")

    supplier.name = name.strip()
    supplier.contact = name.strip()
    supplier.company_name = (company_name or "").strip() or None
    supplier.phone_country_code = (phone_country_code or "+60").strip() or "+60"
    supplier.phone_number = phone_number.strip()
    supplier.address = (address or "").strip() or None

    if category_names:
        categories = [_get_or_create_packaging_category(db, value) for value in category_names]
        supplier.categories = categories
    else:
        supplier.categories = []

    db.commit()
    return {"status": "ok", "id": supplier.id}


@router.delete("/contacts/packaging-suppliers/{supplier_id}")
def delete_packaging_supplier_contact(supplier_id: int, db: Session = Depends(get_db)):
    supplier = db.query(PackagingSupplierContact).filter_by(id=supplier_id).first()
    if not supplier:
        raise HTTPException(404, "Packaging supplier not found")
    db.delete(supplier)
    db.commit()
    return {"status": "ok"}


@router.get("/packaging-purchases")
def list_packaging_purchases(
    supplier_id: Optional[int] = None,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    q = db.query(PackagingPurchase).order_by(PackagingPurchase.purchase_date.desc(), PackagingPurchase.id.desc())
    if supplier_id:
        q = q.filter(PackagingPurchase.packaging_supplier_id == supplier_id)
    total = q.count()
    rows = q.offset(offset).limit(limit).all()
    return {"total": total, "items": [_packaging_purchase_to_dict(row) for row in rows]}


@router.post("/packaging-purchases")
def create_packaging_purchase(
    packaging_supplier_id: int = Form(...),
    purchase_date: str = Form(...),
    qty: int = Form(...),
    unit_cost: float = Form(...),
    product_name: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    category_names_json: Optional[str] = Form(None),
    photo_item: Optional[UploadFile] = File(None),
    photo_receipt: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
):
    from datetime import date as date_type

    supplier = db.query(PackagingSupplierContact).filter_by(id=packaging_supplier_id).first()
    if not supplier:
        raise HTTPException(404, "Packaging supplier not found")

    try:
        pd = date_type.fromisoformat(purchase_date)
    except ValueError:
        raise HTTPException(400, "purchase_date must be YYYY-MM-DD")

    if qty <= 0:
        raise HTTPException(400, "qty must be positive")
    if unit_cost < 0:
        raise HTTPException(400, "unit_cost must be non-negative")
    _enforce_max_decimal_places(unit_cost, "unit_cost", 4)

    selected_category_names = _parse_json_string_list(category_names_json, "category_names_json")
    if not selected_category_names:
        raise HTTPException(400, "At least one category is required")

    available_categories = {category.name.lower(): category for category in (supplier.categories or [])}
    selected_categories = []
    for category_name in selected_category_names:
        category = available_categories.get(category_name.lower())
        if not category:
            raise HTTPException(400, f"Category '{category_name}' is not linked to this supplier")
        selected_categories.append(category)

    purchase = PackagingPurchase(
        packaging_supplier_id=supplier.id,
        supplier_name=supplier.name,
        purchase_date=pd,
        qty=qty,
        unit_cost=unit_cost,
        product_name=(product_name or "").strip() or None,
        notes=(notes or "").strip() or None,
    )
    db.add(purchase)
    db.flush()

    async def _save_packaging_photo(upload: UploadFile, label: str) -> str:
        ext = Path(upload.filename).suffix or ".jpg"
        name = f"pack_{purchase.id}_{label}{ext}"
        dest = PHOTOS_DIR / name
        content = await upload.read()
        dest.write_bytes(content)
        return name

    if photo_item:
        purchase.photo_item = _run_async(_save_packaging_photo(photo_item, "item"))
    if photo_receipt:
        purchase.photo_receipt = _run_async(_save_packaging_photo(photo_receipt, "receipt"))

    purchase.categories = selected_categories
    db.commit()
    db.refresh(purchase)

    return {"status": "ok", "purchase": _packaging_purchase_to_dict(purchase)}


@router.get("/packaging-purchases/{purchase_id}")
def get_packaging_purchase(purchase_id: int, db: Session = Depends(get_db)):
    purchase = db.query(PackagingPurchase).filter_by(id=purchase_id).first()
    if not purchase:
        raise HTTPException(404, "Not found")
    return _packaging_purchase_to_dict(purchase)


@router.delete("/packaging-purchases/{purchase_id}")
def delete_packaging_purchase(purchase_id: int, db: Session = Depends(get_db)):
    purchase = db.query(PackagingPurchase).filter_by(id=purchase_id).first()
    if not purchase:
        raise HTTPException(404, "Not found")
    for attr in ("photo_item", "photo_receipt"):
        filename = getattr(purchase, attr)
        if filename:
            path = PHOTOS_DIR / filename
            if path.exists():
                path.unlink()
    db.delete(purchase)
    db.commit()
    return {"status": "ok"}


@router.get("/contacts/buyers")
def list_buyer_contacts(db: Session = Depends(get_db)):
    rows = db.query(OffPlatformBuyerContact).order_by(OffPlatformBuyerContact.name.asc()).all()
    out = []

    for b in rows:
        top = (
            db.query(OffPlatformSaleItem.product_name, func.sum(OffPlatformSaleItem.qty).label("total_qty"))
            .join(OffPlatformSale, OffPlatformSale.id == OffPlatformSaleItem.sale_id)
            .filter(OffPlatformSale.buyer_contact_id == b.id)
            .group_by(OffPlatformSaleItem.product_name)
            .order_by(func.sum(OffPlatformSaleItem.qty).desc(), OffPlatformSaleItem.product_name.asc())
            .first()
        )
        out.append(
            {
                "id": b.id,
                "name": b.name,
                "company_name": b.company_name,
                "phone_country_code": b.phone_country_code or "+60",
                "phone_number": b.phone_number,
                "address": b.address,
                "most_frequent_product": top[0] if top else None,
                "most_frequent_qty": int(top[1]) if top else 0,
                "created_at": str(b.created_at),
                "updated_at": str(b.updated_at),
            }
        )

    return {"items": out}


@router.post("/contacts/buyers")
def create_buyer_contact(
    name: str = Form(...),
    phone_country_code: Optional[str] = Form("+60"),
    company_name: Optional[str] = Form(None),
    phone_number: str = Form(...),
    address: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    if not name.strip():
        raise HTTPException(400, "name is required")
    if not phone_number.strip():
        raise HTTPException(400, "phone_number is required")

    b = OffPlatformBuyerContact(
        name=name.strip(),
        company_name=(company_name or "").strip() or None,
        phone_country_code=(phone_country_code or "+60").strip() or "+60",
        phone_number=phone_number.strip(),
        address=(address or "").strip() or None,
    )
    db.add(b)
    db.commit()
    return {"status": "ok", "id": b.id}


@router.put("/contacts/buyers/{buyer_id}")
def update_buyer_contact(
    buyer_id: int,
    name: str = Form(...),
    phone_country_code: Optional[str] = Form("+60"),
    company_name: Optional[str] = Form(None),
    phone_number: str = Form(...),
    address: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    if not name.strip():
        raise HTTPException(400, "name is required")
    if not phone_number.strip():
        raise HTTPException(400, "phone_number is required")

    b = db.query(OffPlatformBuyerContact).filter_by(id=buyer_id).first()
    if not b:
        raise HTTPException(404, "Buyer not found")
    b.name = name.strip()
    b.company_name = (company_name or "").strip() or None
    b.phone_country_code = (phone_country_code or "+60").strip() or "+60"
    b.phone_number = phone_number.strip()
    b.address = (address or "").strip() or None
    db.commit()
    return {"status": "ok", "id": b.id}


@router.delete("/contacts/buyers/{buyer_id}")
def delete_buyer_contact(buyer_id: int, db: Session = Depends(get_db)):
    b = db.query(OffPlatformBuyerContact).filter_by(id=buyer_id).first()
    if not b:
        raise HTTPException(404, "Buyer not found")
    db.delete(b)
    db.commit()
    return {"status": "ok"}


@router.get("/purchases")
def list_purchases(
    product_id: Optional[int] = None,
    group_id: Optional[int] = None,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    q = db.query(PurchaseBatch).order_by(PurchaseBatch.purchase_date.desc())
    if group_id:
        from app.models import ProductGroup
        g = db.query(ProductGroup).filter_by(id=group_id).first()
        if not g:
            raise HTTPException(404, "Group not found")
        member_ids = [m.id for m in g.members]
        if not member_ids:
            return {"total": 0, "items": []}
        q = q.filter(PurchaseBatch.product_id.in_(member_ids))
    elif product_id:
        q = q.filter(PurchaseBatch.product_id == product_id)
    total = q.count()
    rows  = q.offset(offset).limit(limit).all()
    return {"total": total, "items": [_batch_to_dict(b) for b in rows]}


@router.post("/purchases")
def create_purchase(
    product_id:    Optional[int]   = Form(None),
    group_id:      Optional[int]   = Form(None),
    purchase_date: str   = Form(...),
    qty:           int   = Form(...),
    unit_cost:     float = Form(...),
    sync_stock:    bool  = Form(True),
    supplier_contact_id: Optional[int] = Form(None),
    supplier_name: Optional[str] = Form(None),
    notes:         Optional[str] = Form(None),
    photo_goods:   Optional[UploadFile] = File(None),
    photo_receipt: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
):
    """
    Record a goods-received batch.
    Immediately pushes the increased stock count to all linked platforms.
    """
    from app.models import Product, ProductGroup
    from datetime import date as date_type

    if not product_id and not group_id:
        raise HTTPException(400, "product_id or group_id is required")

    product = None
    group = None
    batch_product_id = None
    batch_product_name = None
    base_stock = 0

    if group_id:
        group = db.query(ProductGroup).filter_by(id=group_id).first()
        if not group:
            raise HTTPException(404, "Group not found")
        members = sorted(list(group.members), key=lambda x: x.id)
        if not members:
            raise HTTPException(400, "Group has no member products")
        product = members[0]  # anchor product for FIFO batch linkage
        batch_product_id = product.id
        batch_product_name = f"[GROUP] {group.display_name}"
        base_stock = group.master_stock or 0
    else:
        product = db.query(Product).filter_by(id=product_id).first()
        if not product:
            raise HTTPException(404, "Product not found")
        batch_product_id = product.id
        batch_product_name = product.name
        base_stock = product.master_stock or 0

    try:
        pd = date_type.fromisoformat(purchase_date)
    except ValueError:
        raise HTTPException(400, "purchase_date must be YYYY-MM-DD")

    if qty <= 0:
        raise HTTPException(400, "qty must be positive")
    if unit_cost < 0:
        raise HTTPException(400, "unit_cost must be non-negative")
    _enforce_max_decimal_places(unit_cost, "unit_cost", 4)

    supplier_contact = None
    final_supplier_name = (supplier_name or "").strip() or None
    if supplier_contact_id:
        supplier_contact = db.query(SupplierContact).filter_by(id=supplier_contact_id).first()
        if not supplier_contact:
            raise HTTPException(404, "Supplier contact not found")
        if group is None:
            supplied_ids = {
                pid for (pid,) in db.query(supplier_products.c.product_id)
                .filter(supplier_products.c.supplier_contact_id == supplier_contact.id)
                .all()
            }
            if supplied_ids and product.id not in supplied_ids:
                raise HTTPException(400, "Selected supplier is not mapped to this product")
        final_supplier_name = supplier_contact.name

    # Create the batch record first so we have an ID for photo filenames
    batch = PurchaseBatch(
        product_id    = batch_product_id,
        product_name  = batch_product_name,
        purchase_date = pd,
        qty           = qty,
        qty_remaining = qty,
        unit_cost     = unit_cost,
        supplier_name = final_supplier_name,
        supplier_contact_id = supplier_contact.id if supplier_contact else None,
        notes         = (notes or "").strip() or None,
    )
    db.add(batch)
    db.flush()   # get batch.id before saving photos

    # Save photos
    async def _save_photo(upload: UploadFile, label: str) -> str:
        ext  = Path(upload.filename).suffix or ".jpg"
        name = f"{batch.id}_{label}{ext}"
        dest = PHOTOS_DIR / name
        content = await upload.read()
        dest.write_bytes(content)
        return name

    if photo_goods:
        batch.photo_goods = _run_async(
            _save_photo(photo_goods, "goods")
        )
    if photo_receipt:
        batch.photo_receipt = _run_async(
            _save_photo(photo_receipt, "receipt")
        )

    db.commit()
    db.refresh(batch)

    # ── Sync stock (optional) ────────────────────────────────────────────────
    new_stock    = base_stock + qty
    push_results = {}
    if sync_stock:
        try:
            from app.sync_engine import SyncEngine
            engine = SyncEngine()

            if group:
                merged_results: Dict[str, str] = {}
                for member in group.members:
                    member_result = _run_async(
                        engine.push_inventory_for_product(
                            member.id,
                            new_stock,
                            None,
                            db,
                            bdq_override=(group.backorder_display_qty or None),
                        )
                    )
                    for platform, status in member_result.items():
                        prev = merged_results.get(platform)
                        if prev is None:
                            merged_results[platform] = status
                        elif prev == "ok" and status != "ok":
                            merged_results[platform] = status
                push_results = merged_results
                group.master_stock = new_stock
                for member in group.members:
                    member.master_stock = new_stock
            else:
                push_results = _run_async(
                    engine.push_inventory_for_product(product.id, new_stock, None, db)
                )
                # push_inventory_for_product already updates master_stock on full success;
                # do it explicitly here too so it's always consistent
                product.master_stock = new_stock

            batch.stock_pushed = True
        except Exception as e:
            push_results = {"error": str(e)}
            # Still update master stock in DB so it remains consistent with purchase entry
            if group:
                group.master_stock = new_stock
                for member in group.members:
                    member.master_stock = new_stock
            else:
                product.master_stock = new_stock
    else:
        if group:
            group.master_stock = new_stock
            for member in group.members:
                member.master_stock = new_stock
        else:
            product.master_stock = new_stock
        push_results = {"skipped": "sync disabled"}
        batch.stock_pushed = False

    batch.push_results = json.dumps(push_results)
    db.commit()

    return {
        "status":       "ok",
        "batch":        _batch_to_dict(batch),
        "new_stock":    new_stock,
        "push_results": push_results,
    }


@router.get("/purchases/{batch_id}")
def get_purchase(batch_id: int, db: Session = Depends(get_db)):
    b = db.query(PurchaseBatch).filter_by(id=batch_id).first()
    if not b:
        raise HTTPException(404, "Not found")
    return _batch_to_dict(b)


@router.delete("/purchases/{batch_id}")
def delete_purchase(batch_id: int, db: Session = Depends(get_db)):
    b = db.query(PurchaseBatch).filter_by(id=batch_id).first()
    if not b:
        raise HTTPException(404, "Not found")
    # Delete photos from disk
    for attr in ("photo_goods", "photo_receipt"):
        fname = getattr(b, attr)
        if fname:
            p = PHOTOS_DIR / fname
            if p.exists():
                p.unlink()
    db.delete(b)
    db.commit()
    return {"status": "ok"}


@router.get("/purchases/photo/{filename}")
def get_photo(filename: str):
    """Serve a purchase photo by filename."""
    from fastapi.responses import FileResponse
    # Sanitise: only allow plain filenames (no path traversal)
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(400, "Invalid filename")
    path = PHOTOS_DIR / filename
    if not path.exists():
        raise HTTPException(404, "Photo not found")
    return FileResponse(str(path))


@router.get("/off-platform-sales")
def list_off_platform_sales(
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    q = db.query(OffPlatformSale).order_by(OffPlatformSale.sale_date.desc(), OffPlatformSale.id.desc())
    total = q.count()
    rows = q.offset(offset).limit(limit).all()
    return {"total": total, "items": [_off_sale_to_dict(r, db) for r in rows]}


@router.delete("/off-platform-sales/{sale_id}")
def delete_off_platform_sale(sale_id: int, db: Session = Depends(get_db)):
    sale = db.query(OffPlatformSale).filter_by(id=sale_id).first()
    if not sale:
        raise HTTPException(404, "Off-platform sale not found")

    db.query(OffPlatformSaleItem).filter_by(sale_id=sale_id).delete(synchronize_session=False)

    if sale.photo:
        path = PHOTOS_DIR / sale.photo
        if path.exists():
            path.unlink()

    db.delete(sale)
    db.commit()
    return {"status": "ok"}


@router.post("/off-platform-sales")
def create_off_platform_sale(
    sale_date: str = Form(...),
    buyer_contact_id: Optional[int] = Form(None),
    sold_to: Optional[str] = Form(None),
    total_amount: Optional[float] = Form(None),
    sync_stock: bool = Form(True),
    notes: Optional[str] = Form(None),
    items_json: str = Form(...),
    photo: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
):
    """
    Record an off-platform sale with multiple products.
    Deducts stock and pushes new stock to linked platforms.
    items_json format: [{"product_id": int, "qty": int, "unit_price": float}, ...]
    """
    from datetime import date as date_type
    from app.models import Product, ProductGroup

    try:
        sd = date_type.fromisoformat(sale_date)
    except ValueError:
        raise HTTPException(400, "sale_date must be YYYY-MM-DD")

    try:
        raw_items = json.loads(items_json)
    except Exception:
        raise HTTPException(400, "items_json must be valid JSON")

    if not isinstance(raw_items, list) or not raw_items:
        raise HTTPException(400, "At least one sale item is required")

    parsed_items = []
    product_qty_map: Dict[int, int] = defaultdict(int)
    group_qty_map: Dict[int, int] = defaultdict(int)
    computed_total = 0.0

    for idx, row in enumerate(raw_items):
        if not isinstance(row, dict):
            raise HTTPException(400, f"items_json[{idx}] must be an object")
        try:
            qty = int(row.get("qty"))
            unit_price = float(row.get("unit_price"))
        except Exception:
            raise HTTPException(400, f"items_json[{idx}] has invalid qty/unit_price")

        if qty <= 0:
            raise HTTPException(400, f"items_json[{idx}] qty must be > 0")
        if unit_price < 0:
            raise HTTPException(400, f"items_json[{idx}] unit_price must be >= 0")

        pid_raw = row.get("product_id")
        gid_raw = row.get("group_id")

        line_total = round(qty * unit_price, 2)
        computed_total += line_total

        if gid_raw is not None and str(gid_raw) != "":
            try:
                gid = int(gid_raw)
            except Exception:
                raise HTTPException(400, f"items_json[{idx}] has invalid group_id")
            group = db.query(ProductGroup).filter_by(id=gid).first()
            if not group:
                raise HTTPException(404, f"Group not found for items_json[{idx}] id={gid}")
            members = sorted(list(group.members), key=lambda x: x.id)
            if not members:
                raise HTTPException(400, f"Group items_json[{idx}] has no member products")
            anchor = members[0]
            group_qty_map[gid] += qty
            parsed_items.append(
                {
                    "product_id": anchor.id,
                    "product_name": f"[GROUP] {group.display_name}",
                    "qty": qty,
                    "unit_price": unit_price,
                    "line_total": line_total,
                }
            )
        else:
            try:
                pid = int(pid_raw)
            except Exception:
                raise HTTPException(400, f"items_json[{idx}] must include product_id or group_id")
            product = db.query(Product).filter_by(id=pid).first()
            if not product:
                raise HTTPException(404, f"Product not found for items_json[{idx}] id={pid}")
            product_qty_map[pid] += qty
            parsed_items.append(
                {
                    "product_id": pid,
                    "product_name": product.name,
                    "qty": qty,
                    "unit_price": unit_price,
                    "line_total": line_total,
                }
            )

    sale_total = float(total_amount) if total_amount is not None else round(computed_total, 2)

    buyer = None
    final_sold_to = (sold_to or "").strip() or None
    if buyer_contact_id:
        buyer = db.query(OffPlatformBuyerContact).filter_by(id=buyer_contact_id).first()
        if not buyer:
            raise HTTPException(404, "Buyer contact not found")
        final_sold_to = buyer.name

    sale = OffPlatformSale(
        sale_date=sd,
        sold_to=final_sold_to,
        buyer_contact_id=buyer.id if buyer else None,
        total_amount=sale_total,
        notes=(notes or "").strip() or None,
    )
    db.add(sale)
    db.flush()

    if photo:
        ext = Path(photo.filename or "").suffix or ".jpg"
        filename = f"offsale_{sale.id}{ext}"
        dest = PHOTOS_DIR / filename
        content = _run_async(photo.read())
        dest.write_bytes(content)
        sale.photo = filename

    for item in parsed_items:
        db.add(
            OffPlatformSaleItem(
                sale_id=sale.id,
                product_id=item["product_id"],
                product_name=item["product_name"],
                qty=item["qty"],
                unit_price=item["unit_price"],
                line_total=item["line_total"],
            )
        )

    # Deduct stock from selected products and optionally push to linked platforms.
    push_results = {}

    if sync_stock:
        from app.sync_engine import SyncEngine
        engine = SyncEngine()

        for pid, sold_qty in product_qty_map.items():
            product = db.query(Product).filter_by(id=pid).first()
            if not product:
                continue
            new_stock = max(0, int(product.master_stock or 0) - int(sold_qty))
            try:
                result = _run_async(
                    engine.push_inventory_for_product(pid, new_stock, None, db)
                )
                push_results[str(pid)] = result
                product.master_stock = new_stock
            except Exception as e:
                push_results[str(pid)] = {"error": str(e)}
                product.master_stock = new_stock

        for gid, sold_qty in group_qty_map.items():
            group = db.query(ProductGroup).filter_by(id=gid).first()
            if not group:
                continue
            new_stock = max(0, int(group.master_stock or 0) - int(sold_qty))
            merged_results: Dict[str, str] = {}
            try:
                for member in group.members:
                    result = _run_async(
                        engine.push_inventory_for_product(
                            member.id,
                            new_stock,
                            None,
                            db,
                            bdq_override=(group.backorder_display_qty or None),
                        )
                    )
                    for platform, status in result.items():
                        prev = merged_results.get(platform)
                        if prev is None:
                            merged_results[platform] = status
                        elif prev == "ok" and status != "ok":
                            merged_results[platform] = status
                    member.master_stock = new_stock
                group.master_stock = new_stock
                push_results[f"g:{gid}"] = merged_results
            except Exception as e:
                group.master_stock = new_stock
                for member in group.members:
                    member.master_stock = new_stock
                push_results[f"g:{gid}"] = {"error": str(e)}
    else:
        for pid, sold_qty in product_qty_map.items():
            product = db.query(Product).filter_by(id=pid).first()
            if not product:
                continue
            new_stock = max(0, int(product.master_stock or 0) - int(sold_qty))
            product.master_stock = new_stock
            push_results[str(pid)] = {"skipped": "sync disabled"}

        for gid, sold_qty in group_qty_map.items():
            group = db.query(ProductGroup).filter_by(id=gid).first()
            if not group:
                continue
            new_stock = max(0, int(group.master_stock or 0) - int(sold_qty))
            group.master_stock = new_stock
            for member in group.members:
                member.master_stock = new_stock
            push_results[f"g:{gid}"] = {"skipped": "sync disabled"}

    sale.push_results = json.dumps(push_results)
    db.commit()
    db.refresh(sale)

    return {
        "status": "ok",
        "sale": _off_sale_to_dict(sale, db),
        "computed_total": round(computed_total, 2),
        "push_results": push_results,
    }