from sqlalchemy import Column, Integer, String, DateTime, Boolean, ForeignKey, Float, Table
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base

# Association table: many-to-many between ProductGroup and Product
group_members = Table(
    "group_members",
    Base.metadata,
    Column("group_id", Integer, ForeignKey("product_groups.id", ondelete="CASCADE"), primary_key=True),
    Column("product_id", Integer, ForeignKey("products.id", ondelete="CASCADE"), primary_key=True),
)

class ProductGroup(Base):
    __tablename__ = "product_groups"
    id                    = Column(Integer, primary_key=True, index=True)
    display_name          = Column(String, nullable=False)
    master_stock          = Column(Integer, default=0)
    backorder_display_qty = Column(Integer, default=0)  # qty to show on platforms when stock ≤ 0; 0 = feature off
    image_url             = Column(String, nullable=True)
    created_at            = Column(DateTime, server_default=func.now())
    updated_at            = Column(DateTime, server_default=func.now(), onupdate=func.now())
    members               = relationship("Product", secondary=group_members, backref="groups")

class Product(Base):
    __tablename__ = "products"
    id                    = Column(Integer, primary_key=True, index=True)
    master_sku            = Column(String, unique=True, index=True, nullable=False)
    name                  = Column(String, nullable=False)
    master_stock          = Column(Integer, default=0)
    backorder_display_qty = Column(Integer, default=0)  # qty to show on platforms when stock ≤ 0; 0 = feature off
    image_url             = Column(String, nullable=True)
    auto_sync             = Column(Boolean, default=True)
    created_at            = Column(DateTime, server_default=func.now())
    updated_at            = Column(DateTime, server_default=func.now(), onupdate=func.now())
    listings              = relationship("PlatformListing", back_populates="product", cascade="all, delete-orphan")
    sync_logs             = relationship("SyncLog", back_populates="product")

class PlatformListing(Base):
    __tablename__ = "platform_listings"
    id                  = Column(Integer, primary_key=True, index=True)
    product_id          = Column(Integer, ForeignKey("products.id"), nullable=False)
    platform            = Column(String, nullable=False)
    platform_product_id = Column(String, nullable=True)
    platform_sku        = Column(String, nullable=True)
    current_stock       = Column(Integer, default=0)
    last_written_at     = Column(DateTime, nullable=True)
    price               = Column(Float, nullable=True)
    last_synced         = Column(DateTime, nullable=True)
    sync_status         = Column(String, default="pending")
    error_message       = Column(String, nullable=True)
    product             = relationship("Product", back_populates="listings")

class SyncLog(Base):
    __tablename__ = "sync_logs"
    id         = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=True)
    platform   = Column(String, nullable=False)
    action     = Column(String, nullable=False)
    old_stock  = Column(Integer, nullable=True)
    new_stock  = Column(Integer, nullable=True)
    message    = Column(String, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    product    = relationship("Product", back_populates="sync_logs")

class Order(Base):
    __tablename__ = "orders"
    id                  = Column(Integer, primary_key=True, index=True)
    platform            = Column(String, nullable=False)
    platform_order_id   = Column(String, nullable=False, unique=True, index=True)
    status              = Column(String, nullable=False)
    buyer_name          = Column(String, nullable=True)
    total_price         = Column(Float, nullable=True)
    shipping_fee        = Column(Float, nullable=True)
    payment_method      = Column(String, nullable=True)
    items_count         = Column(Integer, nullable=True)
    notified            = Column(Boolean, default=False)
    platform_created_at = Column(DateTime, nullable=True)
    created_at          = Column(DateTime, server_default=func.now())
    updated_at          = Column(DateTime, server_default=func.now(), onupdate=func.now())
    items               = relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")

class OrderItem(Base):
    __tablename__ = "order_items"
    id              = Column(Integer, primary_key=True, index=True)
    order_id        = Column(Integer, ForeignKey("orders.id"), nullable=False)
    platform_sku    = Column(String, nullable=True)
    product_name    = Column(String, nullable=True)
    quantity        = Column(Integer, nullable=True)
    unit_price      = Column(Float, nullable=True)
    order           = relationship("Order", back_populates="items")


class DeviceToken(Base):
    __tablename__ = "device_tokens"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String, nullable=False, index=True)
    token_hash = Column(String(64), nullable=False, unique=True, index=True)
    device_label = Column(String, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    last_used_at = Column(DateTime, server_default=func.now(), nullable=False)
    expires_at = Column(DateTime, nullable=False)