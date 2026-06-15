"""
Financial models — add these to your existing app/models.py
or import from here in your main models file.
"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Float, DateTime, Date, Boolean, ForeignKey, UniqueConstraint, Table
from sqlalchemy.orm import relationship
from app.database import Base


class FinancialTransaction(Base):
    """One row per settled order per platform."""
    __tablename__ = "financial_transactions"

    id              = Column(Integer, primary_key=True, index=True)
    platform        = Column(String, nullable=False)        # shopee | lazada | tiktok
    order_id        = Column(String, nullable=False)
    order_date      = Column(Date, nullable=True)           # when order was placed
    settlement_date = Column(Date, nullable=True)           # when money was released
    month           = Column(String, nullable=False)        # YYYY-MM  (index key)
    year            = Column(Integer, nullable=False)

    # ── Revenue ──────────────────────────────────────────────────
    gross_revenue   = Column(Float, default=0.0)            # product price before deductions
    customer_paid   = Column(Float, default=0.0)            # what buyer actually paid

    # ── Fees (all stored as negative values = cost to seller) ───
    commission_fee  = Column(Float, default=0.0)
    transaction_fee = Column(Float, default=0.0)
    service_fee     = Column(Float, default=0.0)
    other_fees      = Column(Float, default=0.0)            # catch-all for platform-specific
    total_fees      = Column(Float, default=0.0)            # sum of all above

    # ── Shipping ─────────────────────────────────────────────────
    shipping_buyer  = Column(Float, default=0.0)            # buyer paid shipping
    shipping_cost   = Column(Float, default=0.0)            # actual logistic cost (negative)
    shipping_rebate = Column(Float, default=0.0)            # platform subsidy (positive)
    net_shipping    = Column(Float, default=0.0)            # shipping_buyer + shipping_cost + shipping_rebate

    # ── Vouchers / Discounts ─────────────────────────────────────
    voucher_seller  = Column(Float, default=0.0)            # seller-funded vouchers (negative)
    voucher_platform= Column(Float, default=0.0)            # platform-funded (0 impact on seller)

    # ── Final ────────────────────────────────────────────────────
    net_settlement  = Column(Float, default=0.0)            # actual payout received

    # ── Meta ─────────────────────────────────────────────────────
    upload_batch    = Column(String, nullable=True)         # filename of the upload
    created_at      = Column(DateTime, default=datetime.utcnow)

    # ── Product info (best-effort from export) ──────────────────────────
    product_name    = Column(String, nullable=True)
    sku             = Column(String, nullable=True)
    qty             = Column(Integer, default=1)

    __table_args__ = (
        UniqueConstraint("platform", "order_id", name="uq_platform_order"),
    )


class ProductCost(Base):
    """
    Cost price per product — entered manually by the seller.
    Linked to the inventory Product record so SKUs are auto-populated
    from PlatformListing and matching is rock solid.
    """
    __tablename__ = "product_costs"

    id              = Column(Integer, primary_key=True, index=True)

    # ── Link to inventory ────────────────────────────────────────────────────
    product_id      = Column(Integer, nullable=True, index=True)  # FK to products.id
    product_name    = Column(String, nullable=False)              # denormalised for display
    master_sku      = Column(String, nullable=True, index=True)

    # ── Per-platform SKUs (from PlatformListing) ─────────────────────────────
    shopee_sku      = Column(String, nullable=True, index=True)
    lazada_sku      = Column(String, nullable=True, index=True)
    tiktok_sku      = Column(String, nullable=True, index=True)

    # ── Costs ─────────────────────────────────────────────────────────────────
    cost_price      = Column(Float, nullable=False)
    packaging_cost  = Column(Float, default=0.0)
    inbound_cost    = Column(Float, default=0.0)
    notes           = Column(String, nullable=True)
    updated_at      = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @property
    def total_unit_cost(self) -> float:
        return (self.cost_price or 0) + (self.packaging_cost or 0) + (self.inbound_cost or 0)

    def matches_sku(self, platform: str, sku: str) -> bool:
        """Check if a transaction SKU from a given platform matches this cost entry."""
        if not sku:
            return False
        sku = sku.strip().lower()
        if platform == "shopee"  and self.shopee_sku  and self.shopee_sku.strip().lower()  == sku: return True
        if platform == "lazada"  and self.lazada_sku  and self.lazada_sku.strip().lower()  == sku: return True
        if platform == "tiktok"  and self.tiktok_sku  and self.tiktok_sku.strip().lower()  == sku: return True
        if self.master_sku and self.master_sku.strip().lower() == sku: return True
        return False


class PurchaseBatch(Base):
    """
    One record per stock purchase (goods received). Supports FIFO costing.
    When a batch is saved, the stock is pushed to all linked platforms automatically.
    """
    __tablename__ = "purchase_batches"

    id             = Column(Integer, primary_key=True, index=True)

    # ── Product link ──────────────────────────────────────────────────────────
    product_id     = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    product_name   = Column(String, nullable=False)   # denormalised for display

    # ── Purchase details ──────────────────────────────────────────────────────
    purchase_date  = Column(Date, nullable=False)
    qty            = Column(Integer, nullable=False)
    qty_remaining  = Column(Integer, nullable=False)  # for FIFO tracking; decrements as stock is sold
    unit_cost      = Column(Float, nullable=False)    # cost per unit (RM)
    supplier_name  = Column(String, nullable=True)
    supplier_contact_id = Column(Integer, ForeignKey("supplier_contacts.id"), nullable=True, index=True)
    notes          = Column(String, nullable=True)

    # ── Photos ────────────────────────────────────────────────────────────────
    photo_goods    = Column(String, nullable=True)    # relative path under /app/data/purchases/photos/
    photo_receipt  = Column(String, nullable=True)    # relative path

    # ── Stock push result ─────────────────────────────────────────────────────
    stock_pushed   = Column(Boolean, default=False)
    push_results   = Column(String, nullable=True)    # JSON string of {platform: ok/error}

    created_at     = Column(DateTime, default=datetime.utcnow)

    @property
    def total_cost(self) -> float:
        return (self.unit_cost or 0.0) * (self.qty or 0)


class OffPlatformSale(Base):
    """One off-platform sale header (can contain multiple product lines)."""
    __tablename__ = "off_platform_sales"

    id           = Column(Integer, primary_key=True, index=True)
    sale_date    = Column(Date, nullable=False)
    sold_to      = Column(String, nullable=True)
    buyer_contact_id = Column(Integer, ForeignKey("off_platform_buyer_contacts.id"), nullable=True, index=True)
    total_amount = Column(Float, default=0.0)
    notes        = Column(String, nullable=True)
    photo        = Column(String, nullable=True)
    push_results = Column(String, nullable=True)  # JSON string
    created_at   = Column(DateTime, default=datetime.utcnow)


class OffPlatformSaleItem(Base):
    """Line item for an off-platform sale."""
    __tablename__ = "off_platform_sale_items"

    id           = Column(Integer, primary_key=True, index=True)
    sale_id      = Column(Integer, ForeignKey("off_platform_sales.id", ondelete="CASCADE"), nullable=False, index=True)
    product_id   = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    product_name = Column(String, nullable=False)
    qty          = Column(Integer, nullable=False)
    unit_price   = Column(Float, nullable=False)
    line_total   = Column(Float, nullable=False)


supplier_products = Table(
    "supplier_products",
    Base.metadata,
    Column("supplier_contact_id", Integer, ForeignKey("supplier_contacts.id", ondelete="CASCADE"), primary_key=True),
    Column("product_id", Integer, ForeignKey("products.id", ondelete="CASCADE"), primary_key=True),
)


class OffPlatformBuyerContact(Base):
    __tablename__ = "off_platform_buyer_contacts"

    id           = Column(Integer, primary_key=True, index=True)
    name         = Column(String, nullable=False, index=True)
    company_name = Column(String, nullable=True)
    phone_country_code = Column(String, nullable=True, default="+60")
    phone_number = Column(String, nullable=False)
    address      = Column(String, nullable=True)
    created_at   = Column(DateTime, default=datetime.utcnow)
    updated_at   = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SupplierContact(Base):
    __tablename__ = "supplier_contacts"

    id           = Column(Integer, primary_key=True, index=True)
    name         = Column(String, nullable=False, index=True)
    contact      = Column(String, nullable=True)
    company_name = Column(String, nullable=True)
    phone_country_code = Column(String, nullable=True, default="+60")
    phone_number = Column(String, nullable=False)
    address      = Column(String, nullable=True)
    created_at   = Column(DateTime, default=datetime.utcnow)
    updated_at   = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


packaging_supplier_category_links = Table(
    "packaging_supplier_category_links",
    Base.metadata,
    Column("supplier_contact_id", Integer, ForeignKey("packaging_supplier_contacts.id", ondelete="CASCADE"), primary_key=True),
    Column("category_id", Integer, ForeignKey("packaging_supplier_categories.id", ondelete="CASCADE"), primary_key=True),
)


packaging_purchase_category_links = Table(
    "packaging_purchase_category_links",
    Base.metadata,
    Column("purchase_id", Integer, ForeignKey("packaging_purchases.id", ondelete="CASCADE"), primary_key=True),
    Column("category_id", Integer, ForeignKey("packaging_supplier_categories.id", ondelete="CASCADE"), primary_key=True),
)


class PackagingSupplierCategory(Base):
    __tablename__ = "packaging_supplier_categories"

    id           = Column(Integer, primary_key=True, index=True)
    name         = Column(String, nullable=False, unique=True, index=True)
    created_at   = Column(DateTime, default=datetime.utcnow)
    updated_at   = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class PackagingSupplierContact(Base):
    __tablename__ = "packaging_supplier_contacts"

    id           = Column(Integer, primary_key=True, index=True)
    name         = Column(String, nullable=False, index=True)
    contact      = Column(String, nullable=True)
    company_name = Column(String, nullable=True)
    phone_country_code = Column(String, nullable=True, default="+60")
    phone_number = Column(String, nullable=False)
    address      = Column(String, nullable=True)
    created_at   = Column(DateTime, default=datetime.utcnow)
    updated_at   = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    categories = relationship(
        "PackagingSupplierCategory",
        secondary=packaging_supplier_category_links,
        backref="packaging_suppliers",
    )


class PackagingPurchase(Base):
    __tablename__ = "packaging_purchases"

    id           = Column(Integer, primary_key=True, index=True)
    packaging_supplier_id = Column(Integer, ForeignKey("packaging_supplier_contacts.id", ondelete="SET NULL"), nullable=True, index=True)
    supplier_name = Column(String, nullable=True)
    purchase_date = Column(Date, nullable=False)
    qty          = Column(Integer, nullable=False)
    unit_cost    = Column(Float, nullable=False)
    product_name = Column(String, nullable=True)
    notes        = Column(String, nullable=True)
    photo_item   = Column(String, nullable=True)
    photo_receipt = Column(String, nullable=True)
    created_at   = Column(DateTime, default=datetime.utcnow)

    categories = relationship(
        "PackagingSupplierCategory",
        secondary=packaging_purchase_category_links,
        backref="packaging_purchases",
    )

    @property
    def total_cost(self) -> float:
        return (self.unit_cost or 0.0) * (self.qty or 0)


class GeneratedInvoice(Base):
    """
    Persisted invoice document with buyer details and line items.
    Pre-filled with buyer contact information for easy document generation.
    """
    __tablename__ = "generated_invoices"

    id                  = Column(Integer, primary_key=True, index=True)
    buyer_contact_id    = Column(Integer, ForeignKey("off_platform_buyer_contacts.id"), nullable=False, index=True)
    
    # ── Document metadata ─────────────────────────────────────────────
    doc_type            = Column(String, default="invoice")  # invoice | receipt
    invoice_number      = Column(String, nullable=False, unique=True, index=True)  # e.g., "MGT 2603/001"
    invoice_date        = Column(Date, nullable=False)
    payment_terms       = Column(String, nullable=True)  # e.g., "Net 30"
    due_date            = Column(Date, nullable=True)
    currency            = Column(String, default="MYR", nullable=False)  # Currency code
    exchange_rate       = Column(Float, default=1.0, nullable=False)
    
    # ── Amounts ───────────────────────────────────────────────────────
    subtotal            = Column(Float, default=0.0)
    tax_amount          = Column(Float, default=0.0)  # Sales tax / GST
    tax_rate            = Column(Float, default=0.0)  # e.g., 0.06 for 6%
    shipping_cost       = Column(Float, default=0.0)
    discount_amount     = Column(Float, default=0.0)
    total_amount        = Column(Float, default=0.0)
    
    # ── Notes & details ───────────────────────────────────────────────
    remarks             = Column(String, nullable=True)  # Special notes on invoice
    reference_sale_id   = Column(Integer, ForeignKey("off_platform_sales.id"), nullable=True, index=True)  # Link to OffPlatformSale if created from one
    
    # ── Status tracking ───────────────────────────────────────────────
    status              = Column(String, default="draft")  # draft | finalized | paid
    paid_date           = Column(Date, nullable=True)
    notes               = Column(String, nullable=True)
    
    created_at          = Column(DateTime, default=datetime.utcnow)
    updated_at          = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class InvoiceLineItem(Base):
    """Line items for a generated invoice."""
    __tablename__ = "invoice_line_items"

    id              = Column(Integer, primary_key=True, index=True)
    invoice_id      = Column(Integer, ForeignKey("generated_invoices.id", ondelete="CASCADE"), nullable=False, index=True)
    
    # ── Item details ──────────────────────────────────────────────────
    product_id      = Column(Integer, ForeignKey("products.id"), nullable=True, index=True)
    description     = Column(String, nullable=False)
    quantity        = Column(Float, nullable=False)
    unit_price      = Column(Float, nullable=False)
    line_total      = Column(Float, nullable=False)
    
    # ── Optional metadata ─────────────────────────────────────────────
    sku             = Column(String, nullable=True)
    notes           = Column(String, nullable=True)


class GeneratedPurchaseOrder(Base):
    """
    Persisted purchase order document with buyer/vendor details and line items.
    Pre-filled with buyer contact information.
    """
    __tablename__ = "generated_purchase_orders"

    id                  = Column(Integer, primary_key=True, index=True)
    buyer_contact_id    = Column(Integer, ForeignKey("off_platform_buyer_contacts.id"), nullable=False, index=True)
    
    # ── Document metadata ─────────────────────────────────────────────
    po_number           = Column(String, nullable=False, unique=True, index=True)  # e.g., "PO/2603/001"
    po_date             = Column(Date, nullable=False)
    required_date       = Column(Date, nullable=True)  # When delivery is needed
    currency            = Column(String, default="MYR", nullable=False)
    exchange_rate       = Column(Float, default=1.0, nullable=False)
    
    # ── Amounts ───────────────────────────────────────────────────────
    subtotal            = Column(Float, default=0.0)
    tax_amount          = Column(Float, default=0.0)
    tax_rate            = Column(Float, default=0.0)
    shipping_cost       = Column(Float, default=0.0)
    discount_amount     = Column(Float, default=0.0)
    total_amount        = Column(Float, default=0.0)
    
    # ── Order details ─────────────────────────────────────────────────
    delivery_address    = Column(String, nullable=True)
    payment_terms       = Column(String, nullable=True)  # e.g., "Net 30"
    shipping_method     = Column(String, nullable=True)  # e.g., "FOB", "CIF"
    remarks             = Column(String, nullable=True)
    
    # ── Status tracking ───────────────────────────────────────────────
    status              = Column(String, default="draft")  # draft | confirmed | received | cancelled
    confirmed_date      = Column(Date, nullable=True)
    received_date       = Column(Date, nullable=True)
    notes               = Column(String, nullable=True)
    
    created_at          = Column(DateTime, default=datetime.utcnow)
    updated_at          = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class PurchaseOrderLineItem(Base):
    """Line items for a generated purchase order."""
    __tablename__ = "purchase_order_line_items"

    id              = Column(Integer, primary_key=True, index=True)
    po_id           = Column(Integer, ForeignKey("generated_purchase_orders.id", ondelete="CASCADE"), nullable=False, index=True)
    
    # ── Item details ──────────────────────────────────────────────────
    product_id      = Column(Integer, ForeignKey("products.id"), nullable=True, index=True)
    description     = Column(String, nullable=False)
    quantity        = Column(Float, nullable=False)
    unit_price      = Column(Float, nullable=False)
    line_total      = Column(Float, nullable=False)
    
    # ── Optional metadata ─────────────────────────────────────────────
    sku             = Column(String, nullable=True)
    notes           = Column(String, nullable=True)