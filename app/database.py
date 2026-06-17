import logging

from sqlalchemy import create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker
import os
from urllib.parse import urlparse

log = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:////app/db/inventory.db")


def _validate_database_url(url: str) -> None:
    if url.startswith("sqlite:"):
        return

    allow_remote = (os.getenv("ALLOW_REMOTE_DATABASE", "false") or "false").strip().lower() == "true"
    if allow_remote:
        return

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host in {"localhost", "127.0.0.1", "::1"}:
        return

    raise RuntimeError(
        "Remote DATABASE_URL is disabled by default. Set ALLOW_REMOTE_DATABASE=true only for trusted private networks."
    )


_validate_database_url(DATABASE_URL)

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def _run_migrations():
    """Add columns introduced after initial table creation (safe to run multiple times)."""
    migrations = [
        # device auth table
        """
        CREATE TABLE IF NOT EXISTS device_tokens (
            id INTEGER PRIMARY KEY,
            user_id VARCHAR NOT NULL,
            token_hash VARCHAR(64) NOT NULL UNIQUE,
            device_label VARCHAR,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
            last_used_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
            expires_at DATETIME NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_device_tokens_user_id ON device_tokens (user_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_device_tokens_token_hash ON device_tokens (token_hash)",
        # products columns
        "ALTER TABLE products ADD COLUMN backorder_display_qty INTEGER DEFAULT 0",
        # product_groups columns
        "ALTER TABLE product_groups ADD COLUMN backorder_display_qty INTEGER DEFAULT 0",
        # product_costs columns
        "ALTER TABLE product_costs ADD COLUMN product_id INTEGER",
        "ALTER TABLE product_costs ADD COLUMN master_sku VARCHAR",
        "ALTER TABLE product_costs ADD COLUMN shopee_sku VARCHAR",
        "ALTER TABLE product_costs ADD COLUMN lazada_sku VARCHAR",
        "ALTER TABLE product_costs ADD COLUMN tiktok_sku VARCHAR",
        "ALTER TABLE product_costs ADD COLUMN packaging_cost FLOAT DEFAULT 0.0",
        "ALTER TABLE product_costs ADD COLUMN inbound_cost FLOAT DEFAULT 0.0",
        "ALTER TABLE product_costs ADD COLUMN notes VARCHAR",
        "ALTER TABLE product_costs ADD COLUMN updated_at DATETIME",
        # financial_transactions columns added after initial creation
        "ALTER TABLE financial_transactions ADD COLUMN product_name VARCHAR",
        "ALTER TABLE financial_transactions ADD COLUMN sku VARCHAR",
        "ALTER TABLE financial_transactions ADD COLUMN qty INTEGER DEFAULT 1",
        # purchase/off-platform contact linkage columns
        "ALTER TABLE purchase_batches ADD COLUMN supplier_contact_id INTEGER",
        "ALTER TABLE off_platform_sales ADD COLUMN buyer_contact_id INTEGER",
        "ALTER TABLE supplier_contacts ADD COLUMN phone_country_code VARCHAR DEFAULT '+60'",
        "ALTER TABLE off_platform_buyer_contacts ADD COLUMN phone_country_code VARCHAR DEFAULT '+60'",
    ]
    with engine.connect() as conn:
        for sql in migrations:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception as exc:
                message = str(exc).lower()
                if "duplicate column name" in message or "already exists" in message:
                    continue
                log.exception("Unexpected database migration failure for SQL: %s", sql)
                raise

def init_db():
    from app.models import Product, PlatformListing, SyncLog, DeviceToken  # noqa
    from app.financials.models import (  # noqa
        FinancialTransaction,
        ProductCost,
        PurchaseBatch,
        OffPlatformSale,
        OffPlatformSaleItem,
        OffPlatformBuyerContact,
        SupplierContact,
        PackagingSupplierCategory,
        PackagingSupplierContact,
        PackagingPurchase,
    )
    Base.metadata.create_all(bind=engine)
    _run_migrations()
