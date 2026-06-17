"""
Sync engine — the brain of the operation.

Responsibilities:
  - Pull inventory from all 3 platforms
  - Compare against master stock in DB
  - Detect sales (stock decreases) and deduct from all grouped platforms + master
  - Push updates when master stock changes
  - Log every action
"""
import asyncio
import os
import time
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Optional, List, Dict

from sqlalchemy.orm import Session

from app.models import Product, PlatformListing, SyncLog, ProductGroup
from app.sync_log_utils import get_latest_sync_log
from app.marketplace import marketplace_unavailable


class SyncEngine:
    # Temporary diagnostics and guardrails for SG mirrored delta noise.
    DEBUG_SALE_AGGREGATION = os.getenv("SYNC_DEBUG_SALE_AGG", "true").lower() == "true"
    SG_ONLY_TINY_DELTA_MAX = int(os.getenv("SYNC_SG_ONLY_TINY_DELTA_MAX", "2"))
    SG_ONLY_DELTA_COOLDOWN_SECONDS = int(os.getenv("SYNC_SG_ONLY_DELTA_COOLDOWN_SECONDS", "900"))

    # ─── Backorder helper ────────────────────────────────────────────────────

    @staticmethod
    def _effective_push_stock(product: "Product", real_stock: int) -> int:
        """
        Return the stock value to actually send to a platform.
        If real_stock <= 0 and backorder_display_qty > 0, return the display qty
        so buyers can still place orders.  Otherwise return real_stock clamped to 0.
        """
        bdq = getattr(product, "backorder_display_qty", 0) or 0
        if real_stock <= 0 and bdq > 0:
            return bdq
        return max(real_stock, 0)

    @staticmethod
    def _collect_sold_deltas_by_platform(all_products: List["Product"]) -> Dict[str, int]:
        """Return aggregated sold units per platform from sale_detected listings."""
        deltas_by_platform: Dict[str, int] = defaultdict(int)

        for p in all_products:
            for l in p.listings:
                if l.sync_status != "sale_detected":
                    continue
                try:
                    delta = int(l.error_message or 0)
                except Exception:
                    continue
                if delta < 0:
                    deltas_by_platform[l.platform] += abs(delta)

        return dict(deltas_by_platform)

    @staticmethod
    def _compute_total_sold_from_deltas(deltas_by_platform: Dict[str, int]) -> int:
        """
        Compute total sold from per-platform deltas.

        Shopee MY and Shopee SG can mirror each other. When both are present,
        use Shopee MY as the authoritative sale source to avoid double-counting
        the same marketplace movement.
        """
        total_sold = 0

        # Shopee MY/SG linked-shop de-duplication.
        if "shopee" in deltas_by_platform:
            total_sold += deltas_by_platform["shopee"]
        elif "shopee_sg" in deltas_by_platform:
            total_sold += deltas_by_platform["shopee_sg"]

        for platform, sold in deltas_by_platform.items():
            if platform in ("shopee", "shopee_sg"):
                continue
            total_sold += sold

        return total_sold

    @staticmethod
    def _is_sg_only_tiny_delta(deltas_by_platform: Dict[str, int], tiny_max: int) -> bool:
        if not deltas_by_platform:
            return False
        return set(deltas_by_platform.keys()) == {"shopee_sg"} and (deltas_by_platform.get("shopee_sg", 0) <= tiny_max)

    @staticmethod
    def _is_shopee_sg_mirror_lock(platform: str, client) -> bool:
        """Detect Shopee SG mirror-lock write rejection from client metadata."""
        if platform != "shopee_sg":
            return False
        meta = getattr(client, "last_update_meta", None) or {}
        if meta.get("mirror_lock"):
            return True
        msg = f"{meta.get('message', '')} {meta.get('failed_reason', '')}".lower()
        return (
            "auto-sync the update to the corresponding sg product" in msg
            or "edit seller stock in the my shop" in msg
        )

    def _should_skip_sg_only_repeat(
        self,
        db: Session,
        product_ids: List[int],
        sg_delta: int,
    ) -> bool:
        """
        Extra guard: suppress repeated tiny SG-only deductions in a short window.
        This protects against mirrored SG noise loops while preserving real sales.
        """
        cooldown = self.SG_ONLY_DELTA_COOLDOWN_SECONDS
        if cooldown <= 0 or not product_ids:
            return False

        cutoff = datetime.now() - timedelta(seconds=cooldown)
        pattern = f"Auto-deducted {sg_delta} unit(s) after sale%"

        recent = (
            get_latest_sync_log(
                db,
                action="write",
                product_ids=product_ids,
                message_like=pattern,
                created_after=cutoff,
            )
        )
        return recent is not None

    # ─── Full sync (read all platforms) ──────────────────────────────────────

    async def sync_all_inventory(self, db: Session) -> dict:
        """Sync current stock from all supported platforms and update DB."""
        started_at = time.time()
        print("[SyncEngine][sync_all_inventory] START")
        results = {}
        for platform, PlatformClientClass in ():
            results[platform] = await self.sync_platform_inventory(platform, PlatformClientClass, db)

        # After all platforms are synced, detect sales and propagate deductions
        sales_summary = await self._propagate_sales(db)

        elapsed = time.time() - started_at
        total_pulled = sum(r.get("pulled", 0) for r in results.values())
        total_errors = sum(r.get("errors", 0) for r in results.values())
        print(
            "[SyncEngine][sync_all_inventory] DONE "
            f"platforms={len(results)} pulled={total_pulled} errors={total_errors} "
            f"sales_groups={sales_summary.get('processed_groups', 0)} "
            f"sales_units={sales_summary.get('total_sold_units', 0)} "
            f"write_ok={sales_summary.get('write_success', 0)} "
            f"write_fail={sales_summary.get('write_failed', 0)} "
            f"elapsed={elapsed:.2f}s"
        )

        return results

    async def sync_platform_inventory(self, platform: str, PlatformClientClass, db: Session) -> dict:
        """Sync inventory for one platform."""
        started_at = time.time()
        print(f"[SyncEngine][sync_platform_inventory] START platform={platform}")
        client = PlatformClientClass()
        result = {"platform": platform, "pulled": 0, "errors": 0}
        try:
            await client.start()
            if not await client.is_logged_in():
                self._log(db, platform=platform, action="error",
                          message="Not logged in — session expired or missing")
                result["errors"] += 1
                print(f"[SyncEngine][sync_platform_inventory] SKIP platform={platform} reason=not_logged_in")
                return result

            items = await client.get_inventory()
            for item in items:
                self._upsert_listing(db, platform, item)
                result["pulled"] += 1

            self._log(db, platform=platform, action="read",
                      message=f"Pulled {len(items)} products")
        except Exception as e:
            self._log(db, platform=platform, action="error", message=str(e))
            result["errors"] += 1
            print(f"[SyncEngine][sync_platform_inventory] ERROR platform={platform} err={e}")
        finally:
            await client.close()
        elapsed = time.time() - started_at
        print(
            f"[SyncEngine][sync_platform_inventory] DONE platform={platform} "
            f"pulled={result['pulled']} errors={result['errors']} elapsed={elapsed:.2f}s"
        )
        return result

    def _upsert_listing(self, db: Session, platform: str, item: dict):
        """Insert or update a platform listing row. Returns (listing, delta)."""
        sku         = item.get("platform_sku", "")
        platform_id = item.get("platform_product_id", "")
        new_stock   = item.get("stock", 0)

        listing = (
            db.query(PlatformListing)
            .filter_by(platform=platform, platform_sku=sku)
            .first()
        )

        now = datetime.now()
        SYNC_INTERVAL_MINUTES = 35

        if not listing:
            product = db.query(Product).filter_by(master_sku=sku).first()
            if not product:
                product = Product(
                    master_sku=sku or f"{platform}_{platform_id}",
                    name=item.get("name", "Unknown"),
                    master_stock=new_stock,
                )
                db.add(product)
                db.flush()

            listing = PlatformListing(
                product_id=product.id,
                platform=platform,
                platform_product_id=platform_id,
                platform_sku=sku,
            )
            db.add(listing)
            listing.current_stock = new_stock
            listing.price         = item.get("price", 0.0)
            listing.last_synced   = now
            listing.sync_status   = "synced"
            listing.last_written_at = None
            db.commit()
            return

        # DB-only sync state: keep target shown in DB until platform catches up.
        if listing.sync_status == "db_only_synced":
            listing.price = item.get("price", 0.0)
            listing.last_synced = now

            target_stock = int(listing.current_stock or 0)
            observed_stock = int(new_stock or 0)
            if observed_stock >= target_stock:
                listing.current_stock = observed_stock
                listing.sync_status = "synced"
                listing.error_message = None
                self._log(
                    db,
                    platform=platform,
                    action="read",
                    product_id=listing.product_id,
                    old_stock=target_stock,
                    new_stock=observed_stock,
                    message="Platform catch-up confirmed after DB-only sync",
                )
            else:
                listing.error_message = (
                    f"DB-only sync pending (platform={observed_stock}, target={target_stock})"
                )
            db.commit()
            return

        # Write-lock/dirty flag: skip sale detection if recently written
        if listing.last_written_at and (now - listing.last_written_at).total_seconds() < SYNC_INTERVAL_MINUTES * 60:
            listing.current_stock = new_stock
            listing.price         = item.get("price", 0.0)
            listing.last_synced   = now
            listing.sync_status   = "synced"
            db.commit()
            return

        # Failed write recovery: refresh pull baseline without treating it as a sale.
        # Keep out_of_sync so the scheduler can continue retrying push-all-out-of-sync.
        if listing.sync_status == "out_of_sync":
            listing.current_stock = new_stock
            listing.price         = item.get("price", 0.0)
            listing.last_synced   = now
            listing.error_message = None
            db.commit()
            return

        # Calculate delta — negative means a sale occurred
        old_stock = listing.current_stock or 0
        delta     = new_stock - old_stock  # negative = units sold

        if delta < 0:
            # Mark listing with how many units were sold so _propagate_sales can act
            listing.sync_status   = "sale_detected"
            listing.error_message = str(delta)  # store delta temporarily
            self._log(db, platform=platform, action="read",
                      product_id=listing.product_id,
                      old_stock=old_stock, new_stock=new_stock,
                      message=f"Sale detected: {abs(delta)} unit(s) sold on {platform}")

        listing.current_stock = new_stock
        listing.price         = item.get("price", 0.0)
        listing.last_synced   = now
        if delta >= 0:
            listing.sync_status = "synced"
        db.commit()

    async def _propagate_sales(self, db: Session):
        """
        After a full pull, find any sale_detected listings and:
        1. Sum up all sold units across all platforms for each group
        2. Deduct from master stock
        3. Push new stock to all other platforms in the group
        Implements: immediate baseline update, per-platform baseline, write-lock, multi-platform sanity check.
        """
        summary = {
            "groups_seen": 0,
            "processed_groups": 0,
            "total_sold_units": 0,
            "write_success": 0,
            "write_failed": 0,
            "suppressed_groups": 0,
        }

        sale_listings = (
            db.query(PlatformListing)
            .filter_by(sync_status="sale_detected")
            .all()
        )
        if not sale_listings:
            return summary

        print(f"[SyncEngine][propagate_sales] START sale_listings={len(sale_listings)}")

        processed_groups = set()

        for listing in sale_listings:
            product = db.query(Product).filter_by(id=listing.product_id).first()
            if not product:
                continue

            group = None
            for g in product.groups:
                group = g
                break

            if group:
                group_key = f"group_{group.id}"
            else:
                group_key = f"product_{product.id}"

            if group_key in processed_groups:
                continue
            processed_groups.add(group_key)
            summary["groups_seen"] += 1

            if group:
                all_products = group.members
                current_master = group.master_stock
                product_ids = [p.id for p in all_products]
            else:
                all_products = [product]
                current_master = product.master_stock
                product_ids = [product.id]

            # Multi-platform sale sanity check: only process largest delta
            group_sale_listings = [
                l for p in all_products for l in p.listings if l.sync_status == "sale_detected"
            ]
            if len(group_sale_listings) > 1:
                deltas = [(l, int(l.error_message or 0)) for l in group_sale_listings]
                deltas.sort(key=lambda x: x[1])  # most negative first
                main_listing, main_delta = deltas[0]
                self._log(
                    db,
                    platform="MULTI",
                    action="warning",
                    message=f"Multiple platforms detected sales in same cycle: {[l.platform for l, _ in deltas]}. Only processing {main_listing.platform}."
                )
                for l, _ in deltas[1:]:
                    l.sync_status = "synced"
                    l.error_message = None
                db.commit()
                group_sale_listings = [main_listing]

            deltas_by_platform = self._collect_sold_deltas_by_platform(all_products)

            if self.DEBUG_SALE_AGGREGATION and deltas_by_platform:
                print(
                    f"[SyncEngine][DEBUG] key={group_key} "
                    f"deltas={dict(sorted(deltas_by_platform.items()))}"
                )

            total_sold = self._compute_total_sold_from_deltas(deltas_by_platform)

            if total_sold == 0:
                continue

            if self._is_sg_only_tiny_delta(deltas_by_platform, self.SG_ONLY_TINY_DELTA_MAX):
                sg_delta = deltas_by_platform.get("shopee_sg", 0)
                if self._should_skip_sg_only_repeat(db, product_ids, sg_delta):
                    print(
                        f"[SyncEngine][GUARD] Suppressed SG-only repeated deduction "
                        f"key={group_key} delta={sg_delta} cooldown={self.SG_ONLY_DELTA_COOLDOWN_SECONDS}s"
                    )
                    for p in all_products:
                        for l in p.listings:
                            if l.sync_status == "sale_detected":
                                l.sync_status = "synced"
                                l.error_message = None
                    db.commit()
                    summary["suppressed_groups"] += 1
                    continue

            new_master = max(0, current_master - total_sold)
            print(f"[SyncEngine] Sale detected — {total_sold} unit(s) sold. "
                  f"Master stock: {current_master} → {new_master}")
            summary["processed_groups"] += 1
            summary["total_sold_units"] += total_sold

            if group:
                group.master_stock = new_master
            else:
                product.master_stock = new_master

            now = datetime.now()
            for p in all_products:
                for l in p.listings:
                    ctx_obj = group if group else p
                    push_qty = self._effective_push_stock(ctx_obj, new_master)
                    PlatformClientClass = None
                    if not PlatformClientClass:
                        continue
                    client = PlatformClientClass()
                    try:
                        await client.start()
                        if not await client.is_logged_in():
                            l.sync_status = "out_of_sync"
                            l.error_message = "Not logged in during auto-deduct push"
                            summary["write_failed"] += 1
                            self._log(
                                db,
                                platform=l.platform,
                                action="error",
                                product_id=p.id,
                                message="Not logged in during auto-deduct push",
                            )
                            continue
                        success = await client.update_stock(
                            l.platform_product_id, push_qty, l.platform_sku
                        )
                        if success:
                            l.current_stock = push_qty
                            l.sync_status = "synced"
                            l.last_synced = now
                            l.last_written_at = now
                            l.error_message = None
                            summary["write_success"] += 1
                            msg = f"Auto-deducted {total_sold} unit(s) after sale"
                            if push_qty != new_master:
                                msg += f" (backorder display: {push_qty})"
                            self._log(db, platform=l.platform, action="write",
                                      product_id=p.id,
                                      old_stock=None, new_stock=push_qty,
                                      message=msg)
                        else:
                            if self._is_shopee_sg_mirror_lock(l.platform, client):
                                l.current_stock = push_qty
                                l.sync_status = "db_only_synced"
                                l.last_synced = now
                                l.error_message = (
                                    "DB-only sync: Shopee SG locked; waiting MY->SG auto-sync"
                                )
                                summary["write_success"] += 1
                                self._log(
                                    db,
                                    platform=l.platform,
                                    action="write",
                                    product_id=p.id,
                                    old_stock=None,
                                    new_stock=push_qty,
                                    message=(
                                        "DB-only sync applied after Shopee SG mirror lock "
                                        "(platform update blocked; waiting MY->SG auto-sync)"
                                    ),
                                )
                            else:
                                l.sync_status = "out_of_sync"
                                l.error_message = "Auto-deduct push failed"
                                summary["write_failed"] += 1
                                self._log(
                                    db,
                                    platform=l.platform,
                                    action="error",
                                    product_id=p.id,
                                    message="Auto-deduct push failed",
                                )
                    except Exception as e:
                        l.sync_status = "out_of_sync"
                        l.error_message = str(e)
                        summary["write_failed"] += 1
                        self._log(db, platform=l.platform, action="error",
                                  product_id=p.id, message=str(e))
                    finally:
                        await client.close()
            db.commit()

        print(
            "[SyncEngine][propagate_sales] DONE "
            f"groups_seen={summary['groups_seen']} processed={summary['processed_groups']} "
            f"suppressed={summary['suppressed_groups']} sold_units={summary['total_sold_units']} "
            f"write_ok={summary['write_success']} write_fail={summary['write_failed']}"
        )
        return summary

    # ─── Push: update stock on platforms ─────────────────────────────────────

    async def push_inventory_for_product(self, product_id: int, new_stock: int,
                           platforms: Optional[List[str]], db: Session,
                           bdq_override: Optional[int] = None) -> dict:
        """
        Push a new stock value to one or more platforms for a given product.
        If platforms is None, push to all linked platforms.
        bdq_override: if set, use this backorder_display_qty instead of the product's own.
        """
        product = db.query(Product).filter_by(id=product_id).first()
        if not product:
            return {"error": "Product not found"}

        target_platforms = platforms or [l.platform for l in product.listings]
        results = {}
        print(
            f"[SyncEngine][push_inventory_for_product] START product_id={product_id} "
            f"master_target={new_stock} target_platforms={target_platforms}"
        )

        for platform in target_platforms:
            listing = next(
                (l for l in product.listings if l.platform == platform), None
            )
            if not listing:
                results[platform] = "no listing"
                continue

            old_stock    = listing.current_stock
            # Use bdq_override (group-level) if provided, else product-level
            effective_bdq = bdq_override if bdq_override is not None else (getattr(product, "backorder_display_qty", 0) or 0)
            push_qty = new_stock if new_stock > 0 else (effective_bdq if effective_bdq > 0 else 0)
            PlatformClientClass = None
            if not PlatformClientClass:
                continue

            client = PlatformClientClass()
            success = False
            try:
                await client.start()
                if not await client.is_logged_in():
                    listing.sync_status   = "error"
                    listing.error_message = "Not logged in"
                    self._log(db, platform=platform, action="error",
                              product_id=product_id,
                              message="Not logged in during push")
                    results[platform] = "not_logged_in"
                    continue

                success = await client.update_stock(
                    listing.platform_product_id, push_qty, listing.platform_sku
                )
            except Exception as e:
                listing.error_message = str(e)
                self._log(db, platform=platform, action="error",
                          product_id=product_id, message=str(e))
            finally:
                await client.close()

            if success:
                listing.current_stock = push_qty
                listing.sync_status   = "synced"
                listing.last_synced   = datetime.now()
                listing.error_message = None
                msg = "Stock updated successfully"
                if push_qty != new_stock:
                    msg += f" (backorder display: {push_qty}, real: {new_stock})"
                self._log(db, platform=platform, action="write",
                          product_id=product_id,
                          old_stock=old_stock, new_stock=push_qty,
                          message=msg)
            else:
                if self._is_shopee_sg_mirror_lock(platform, client):
                    listing.current_stock = push_qty
                    listing.sync_status = "db_only_synced"
                    listing.last_synced = datetime.now()
                    listing.error_message = (
                        "DB-only sync: Shopee SG locked; waiting MY->SG auto-sync"
                    )
                    self._log(
                        db,
                        platform=platform,
                        action="write",
                        product_id=product_id,
                        old_stock=old_stock,
                        new_stock=push_qty,
                        message=(
                            "DB-only sync applied after Shopee SG mirror lock "
                            "(platform update blocked; waiting MY->SG auto-sync)"
                        ),
                    )
                    results[platform] = "db_only_synced"
                    continue
                listing.sync_status = "error"

            results[platform] = "ok" if success else "error"

        # Update master stock; allow negative (backorder)
        product.master_stock = new_stock

        db.commit()
        ok_count = sum(1 for v in results.values() if v == "ok")
        db_only_count = sum(1 for v in results.values() if v == "db_only_synced")
        print(
            f"[SyncEngine][push_inventory_for_product] DONE product_id={product_id} "
            f"ok={ok_count} db_only={db_only_count} total={len(results)} "
            f"master_stock={product.master_stock}"
        )
        return results

    async def push_all_out_of_sync(self, db: Session) -> dict:
        """Push master_stock to any platform listing that is out of sync."""
        started_at = time.time()
        out_of_sync = (
            db.query(PlatformListing)
            .join(Product)
            .filter(PlatformListing.sync_status == "out_of_sync")
            .all()
        )
        print(f"[SyncEngine][push_all_out_of_sync] START listings={len(out_of_sync)}")
        # Push once per product so linked platforms (Shopee MY/SG) can be included together.
        by_product: dict[int, dict] = {}
        for listing in out_of_sync:
            pid = listing.product.id
            if pid not in by_product:
                by_product[pid] = {
                    "product": listing.product,
                    "platforms": set(),
                }
            by_product[pid]["platforms"].add(listing.platform)

        results = []
        for row in by_product.values():
            product = row["product"]
            requested = set(row["platforms"])
            if "shopee" in requested or "shopee_sg" in requested:
                requested |= {"shopee", "shopee_sg"}

            r = await self.push_inventory_for_product(
                product.id,
                product.master_stock,
                list(requested),
                db,
            )
            results.append({"product": product.name, **r})
        elapsed = time.time() - started_at
        print(
            f"[SyncEngine][push_all_out_of_sync] DONE products={len(by_product)} "
            f"elapsed={elapsed:.2f}s"
        )
        return {"synced": results}

    # ─── Helpers ─────────────────────────────────────────────────────────────

    def _log(self, db: Session, platform: str, action: str,
             product_id: int = None, old_stock: int = None,
             new_stock: int = None, message: str = None):
        entry = SyncLog(
            platform=platform,
            action=action,
            product_id=product_id,
            old_stock=old_stock,
            new_stock=new_stock,
            message=message,
        )
        try:
            db.add(entry)
            db.commit()
        except Exception as exc:
            db.rollback()
            print(f"[SyncEngine][_log] skipped due to DB error: {exc}")