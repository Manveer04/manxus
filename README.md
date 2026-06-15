<div align="center">
  <img src="Manxus_Logo.png" alt="Manxus Logo" width="25%">

  # Manxus

  **Internal Operations Platform for Manzill Globe Trading**

  Manxus is a comprehensive full-stack operations platform designed to streamline the daily operations of Manzill Globe Trading, a Malaysian online grocery retailer operating across Shopee, Lazada, and TikTok Shop.
</div>

---

## Platform Overview

The platform is composed of four integrated modules—**InvSync**, **ProcSync**, **DocFlow**, and **RevLens**—that work together to automate inventory management, order processing, document workflows, and financial analytics.

---

## Platform Components

### 1. InvSync — Inventory Synchronization Engine
**Tech Stack:** Python (FastAPI), SQLAlchemy, Playwright, APScheduler

Real-time, bi-directional inventory management across all three marketplaces.

**Key Features:**
- **Pull** real-time stock levels from Shopee, Lazada, and TikTok Shop seller centers
- **Push** instant updates when master stock changes (no API key required—uses browser automation)
- **Detect** sales automatically and deduct from all grouped platforms + master inventory
- **Group** products to manage multiple SKUs as a single master inventory unit
- **Log** every sync action with timestamps, deltas, and error handling
- **Handle** backorder states (show limited qty when stock ≤ 0)
- **Manage** Shopee Malaysia ↔ Singapore shop linking (deduplication logic)

**Data Model:**
- ProductGroup: Logical inventory groupings
- Product: Individual SKU with master stock
- PlatformListing: Per-platform variant with current stock, price, SKU
- SyncLog: Complete audit trail of every action

---

### 2. ProcSync — Order Processing & Logistics
**Tech Stack:** Python (FastAPI), APScheduler, HTTPx, Playwright

Automated order fulfillment and shipment orchestration.

**Key Features:**
- **Auto-fetch** new orders from all three platforms every N minutes
- **Track** order state transitions (pending → processing → shipped → delivered)
- **Generate** shipping manifests and AWB (airway bill) references
- **Integrate** with Shopee/Lazada APIs for arrangement of shipments
- **Monitor** unshipped orders and send notifications to staff
- **Retry** failed order syncs with exponential backoff
- **Support** email-based order triggers (Yahoo/Gmail integration)

**Order Lifecycle:**
```
New Order → Fetch Details → Assign to Shipment → 
Generate Manifest → Create AWB → Update Status → 
Track Delivery → Mark Complete
```

---

### 3. DocFlow — Document Management & Financial Workflows
**Tech Stack:** Python (FastAPI), OpenPyXL, image parsing, SQLAlchemy

Centralized management of invoices, purchase orders, receipts, and supplier communications.

**Key Features:**
- **Generate** invoices to customers with line items and totals
- **Create** purchase orders (POs) to suppliers with tracked dates and amounts
- **Parse** supplier receipts and purchase invoices automatically
- **Track** packaging suppliers and their product categories
- **Store** receipt photos with purchase batch metadata
- **Manage** supplier contacts and payment terms
- **Audit** all document creation and modification with timestamps

**Document Types:**
- Customer Invoices (generated, with COGS calculations)
- Purchase Orders (supplier-facing)
- Supplier Receipts (parsed and catalogued)
- Purchase Batches (grouped purchases with photos)

---

### 4. RevLens — Financial Analytics & Reporting
**Tech Stack:** Python (SQLAlchemy), Financial models, Report generation

Real-time visibility into profitability, COGS, and financial transactions.

**Key Features:**
- **Track** all financial transactions (sales, purchases, expenses)
- **Calculate** Cost of Goods Sold (COGS) using FIFO or LIFO method
- **Support** COGS method switching on a configurable date
- **Generate** supplier profitability reports
- **Categorize** off-platform sales (wholesale, retail, returns)
- **Monitor** product costs and margin health
- **Export** financial statements and tax-ready reports

**Financial Models:**
- FinancialTransaction: Every income/expense ledger entry
- ProductCost: Cost tracking per SKU
- PurchaseBatch: Grouped supplier purchases
- GeneratedInvoice & GeneratedPurchaseOrder: Templated document generation

---

## System Architecture

```
┌─────────────────────────────────────────────────────────┐
│                  Manxus Platform                        │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐ │
│  │   InvSync    │  │  ProcSync    │  │   DocFlow    │ │
│  │ Inventory    │  │ Orders &     │  │ Documents &  │ │
│  │ Sync Engine  │  │ Shipments    │  │ Workflows    │ │
│  └──────────────┘  └──────────────┘  └──────────────┘ │
│         │                 │                  │          │
│         ├─────────────────┼──────────────────┤          │
│         │                 │                  │          │
│         └─────────────────┴──────────────────┘          │
│                  ▼ (shared data)                        │
│         ┌────────────────────────────┐                 │
│         │   RevLens Analytics        │                 │
│         │ Financial Reporting &      │                 │
│         │ COGS Calculations          │                 │
│         └────────────────────────────┘                 │
│                                                         │
│         ┌────────────────────────────┐                 │
│         │  Unified SQLite Database   │                 │
│         │  (or PostgreSQL)           │                 │
│         └────────────────────────────┘                 │
└─────────────────────────────────────────────────────────┘

External Integrations:
  • Shopee Seller Center (Playwright automation + API)
  • Lazada Seller Center (Playwright automation + API)
  • TikTok Shop Seller Center (Playwright automation)
  • Email (Yahoo/Gmail for order triggers & notifications)
  • Ntfy.sh (push alerts)
```

---

## Quick Start

### Prerequisites
- Docker & Docker Compose (or Python 3.11+ for local development)
- Seller center access to Shopee, Lazada, and/or TikTok Shop
- Email account (Yahoo/Gmail) for notifications

### 1. Deploy with Docker

```bash
# Clone the repository
git clone <repository-url>
cd manxus

# Create .env from template
cp .env.example .env

# Edit .env with your seller center credentials and settings
nano .env

# Start Manxus
docker compose up -d
```

**Access the dashboard:** http://localhost:8080

### 2. Initial Platform Login

Before syncing, you must log in to each seller platform. Sessions are stored in `./sessions/` volume.

```bash
# Log in to Shopee (example)
docker compose exec inventory-sync python - <<'EOF'
import asyncio
from app.scrapers import SCRAPERS

async def login_shopee():
    scraper = SCRAPERS.get("shopee")
    if not scraper:
        print("Scrapers not configured")
        return
    
    s = scraper()
    await s.start(headless=False)  # Browser window appears
    input("Log in to Shopee, then press Enter...")
    await s.save_session()
    await s.close()
    print("✓ Shopee session saved!")

asyncio.run(login_shopee())
EOF
```

Repeat for Lazada and TikTok Shop as needed.

### 3. Verify Configuration

```bash
# Check logs
docker compose logs inventory-sync

# Manually trigger a sync
curl -X POST http://localhost:8080/api/sync/pull

# View sync results
curl http://localhost:8080/api/sync/logs
```

---

## Configuration

### Core Settings

```env
# Server & Database
DATABASE_URL=sqlite:////app/db/inventory.db
PUBLIC_BASE_URL=http://localhost:8080
ORDER_ACTION_BASE_URL=http://inventory-sync:8000

# Sync Scheduling
SYNC_INTERVAL_MINUTES=30                  # InvSync pull frequency
ORDER_INTERVAL_MINUTES=10                 # ProcSync order fetch frequency
NOTIFY_RETRY_INTERVAL_MINUTES=5
AUTO_SYNC_ENABLED=true
ORDER_SYNC_ON_STARTUP=true
```

### Shopee Integration

```env
SHOPEE_APP_KEY=<your-app-key>
SHOPEE_APP_SECRET=<your-app-secret>
SHOPEE_MAIN_SHOP_ID=<main-shop-id>
SHOPEE_SG_SHOP_ID=<sg-shop-id>
SHOPEE_ACCESS_TOKEN=<token>
SHOPEE_REFRESH_TOKEN=<refresh-token>
```

### Lazada Integration

```env
LAZADA_APP_KEY=<your-app-key>
LAZADA_APP_SECRET=<your-app-secret>
LAZADA_AUTH_CODE=<auth-code>
```

### TikTok Shop Integration

```env
TIKTOK_APP_KEY=<your-app-key>
TIKTOK_APP_SECRET=<your-app-secret>
TIKTOK_AUTH_CODE=<auth-code>
```

### Email & Notifications

```env
YAHOO_EMAIL=operations@manzill.my
YAHOO_APP_PASSWORD=<app-password>
NTFY_TOPIC=manxus-alerts               # Optional: ntfy.sh alerts
```

### Financial Settings

```env
# COGS Calculation
COGS_LIFO_SWITCH_DATE=2024-01-01        # Switch from FIFO to LIFO on this date

# Security & Auth
APP_BASIC_AUTH_USER=admin
APP_BASIC_AUTH_PASSWORD=<secure-password>
REQUIRE_API_AUTH=true
TOKEN_ENCRYPTION_KEY=<random-key>
```

---

## API Endpoints

### InvSync (Inventory)
- `GET /api/products` — List all products
- `POST /api/products` — Create new product
- `PUT /api/products/{id}` — Update product
- `GET /api/products/{id}/listings` — View platform listings
- `POST /api/sync/pull` — Force pull from all platforms
- `POST /api/sync/push/{product_id}` — Force push to platforms
- `GET /api/sync/logs` — View sync history

### ProcSync (Orders)
- `GET /api/orders` — List all orders
- `GET /api/orders/{id}` — Get order details
- `POST /api/orders/{id}/shipment` — Arrange shipment
- `GET /api/orders/{id}/tracking` — Get tracking number

### DocFlow (Documents)
- `GET /api/financials/transactions` — View transactions
- `POST /api/financials/invoices` — Generate invoice
- `POST /api/financials/purchase-orders` — Create PO
- `GET /api/financials/suppliers` — List suppliers
- `POST /api/financials/suppliers/{id}/contacts` — Add supplier contact

### RevLens (Analytics)
- `GET /api/financials/cogs` — Calculate COGS
- `GET /api/financials/reports/profitability` — Profit by supplier
- `GET /api/financials/reports/margin` — Product margins
- `GET /api/financials/reports/tax` — Tax-ready report

---

## Project Structure

```
manxus/
├── app/
│   ├── main.py                    # FastAPI app
│   ├── database.py                # SQLAlchemy setup
│   ├── models.py                  # Core data models
│   │
│   ├── sync_engine.py             # ← InvSync core
│   ├── order_engine.py            # ← ProcSync core
│   ├── scheduler.py               # Background jobs (APScheduler)
│   ├── email_watcher.py           # Email order triggers
│   ├── notifier.py                # Notification delivery
│   │
│   ├── api/
│   │   └── routes.py              # REST endpoints
│   │
│   ├── financials/                # ← DocFlow & RevLens
│   │   ├── models.py              # Financial models
│   │   ├── routes.py              # Financial endpoints
│   │   ├── document_routes.py     # Invoice/PO endpoints
│   │   └── parsers.py             # Receipt parsers
│   │
│   ├── *_auth.py                  # OAuth handlers
│   ├── security_utils.py          # Auth & encryption
│   ├── static/                    # Frontend (HTML/JS)
│   └── ...
│
├── data/
│   ├── purchases/photos/          # Receipt images
│   └── inspect_*.py               # Debug utilities
│
├── db/
│   ├── inventory.db               # SQLite (production)
│   └── migrations/                # Alembic scripts
│
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
└── README.md
```

---

## Database Schema

### Inventory (InvSync)
- **ProductGroup**: Master inventory groupings
- **Product**: Individual SKU with master_stock
- **PlatformListing**: Product presence on Shopee/Lazada/TikTok (current_stock, platform_sku, price)
- **SyncLog**: Complete audit of every sync action

### Orders (ProcSync)
- **Order**: Marketplace order with buyer info, platform_id, order_status
- **OrderItem**: Line items with quantity, price, SKU
- **Shipment**: Tracking info, AWB, logistics provider

### Financial (DocFlow & RevLens)
- **FinancialTransaction**: Ledger entries (amount, type, date)
- **PurchaseBatch**: Supplier purchases with metadata and photos
- **GeneratedInvoice**: Customer invoices with line items
- **GeneratedPurchaseOrder**: Supplier POs
- **SupplierContact**: Supplier information
- **PackagingSupplier**: Packaging material vendors

---

## Common Tasks

### Pull Inventory from All Platforms

```bash
# Trigger manually via API
curl -X POST http://localhost:8080/api/sync/pull

# Or set AUTO_SYNC_ENABLED=true and it runs every SYNC_INTERVAL_MINUTES
```

### Create a Product Group

Use the dashboard at **http://localhost:8080** or via API:

```bash
curl -X POST http://localhost:8080/api/products/groups \
  -H "Content-Type: application/json" \
  -d '{
    "display_name": "Organic Rice 5kg",
    "master_stock": 100,
    "backorder_display_qty": 5
  }'
```

### Generate an Invoice

```bash
curl -X POST http://localhost:8080/api/financials/invoices \
  -H "Content-Type: application/json" \
  -d '{
    "order_id": 123,
    "line_items": [
      {"description": "Organic Rice 5kg", "qty": 2, "price": 25.00}
    ]
  }'
```

### Check COGS Report

```bash
curl http://localhost:8080/api/financials/reports/cogs \
  -H "Authorization: Bearer <token>"
```

---

## Troubleshooting

### "Session expired" / "Not logged in" errors

**Cause:** Browser cookies have expired or been cleared

**Solution:** Re-run the login procedure:

```bash
docker compose exec inventory-sync python - <<'EOF'
import asyncio
from app.scrapers import SCRAPERS

async def relogin():
    for platform_name in ["shopee", "lazada", "tiktok"]:
        scraper = SCRAPERS.get(platform_name)
        if not scraper:
            continue
        s = scraper()
        await s.start(headless=False)
        input(f"Log in to {platform_name.upper()}, then press Enter...")
        await s.save_session()
        await s.close()
        print(f"✓ {platform_name} session saved")

asyncio.run(relogin())
EOF
```

### Sync stuck or not pulling inventory

**Checks:**
1. Verify credentials in `.env` file
2. Check browser sessions are still valid (re-login if needed)
3. Review logs: `docker compose logs inventory-sync`
4. Manually test: `curl -X POST http://localhost:8080/api/sync/pull`
5. Check database: `sqlite3 ./db/inventory.db "SELECT * FROM sync_logs ORDER BY created_at DESC LIMIT 10;"`

### "Selector not found" errors (UI automation broke)

**Cause:** Seller center UI changed, CSS selectors are outdated

**Solution:** Update selectors:
1. Open seller center in browser
2. Right-click on the element (e.g., stock input) → **Inspect**
3. Find stable attributes: `data-testid`, `name`, `aria-label`
4. Update the selector in the relevant scraper file
5. Test with: `python -c "...scraper test code..."`

### Database locked

```bash
# Restart the app
docker compose restart inventory-sync
```

### High memory usage

Playwright uses 2GB of shared memory. Adjust in `docker-compose.yml`:

```yaml
services:
  inventory-sync:
    shm_size: "3gb"  # Increase if needed
```

---

## Development

### Local Setup (Without Docker)

```bash
# Create virtual environment
python3.11 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Set environment variables
export DATABASE_URL=sqlite:///inventory.db
export PUBLIC_BASE_URL=http://localhost:8000
export SHOPEE_APP_KEY=...
# ... other env vars ...

# Run the server
python -m uvicorn app.main:app --reload
```

### Testing a Sync Cycle

```python
import asyncio
from app.sync_engine import SyncEngine
from app.database import SessionLocal

async def test_sync():
    db = SessionLocal()
    engine = SyncEngine()
    
    # Pull from all platforms
    results = await engine.pull_all(db)
    print(f"Pulled: {results}")
    
    # Check for sales
    await engine.detect_and_push(db)
    
    db.close()

asyncio.run(test_sync())
```

---

## Performance Tuning

| Setting | Recommendation | Rationale |
|---------|-----------------|-----------|
| `SYNC_INTERVAL_MINUTES` | 30–60 | Balance between freshness and API rate limits |
| `ORDER_INTERVAL_MINUTES` | 5–10 | Real-time order visibility for fulfillment |
| `DATABASE_URL` | SQLite for single instance; PostgreSQL for multi-instance | SQLite fine for retail operations |
| `shm_size` | 2–3 GB | Playwright browser automation memory |

---

## Deployment (TrueNAS / Kubernetes / Cloud)

### TrueNAS

```bash
cd /mnt/tank/docker/manxus
docker compose up -d
```

### Kubernetes

See `k8s/` directory for Helm charts (coming soon).

### Cloud (AWS/GCP/Azure)

Use managed database (RDS PostgreSQL), environment variables for secrets, and load balancers for HTTPS termination.

---

---

## Support & Contributions

For questions, bugs, or feature requests:
1. Check logs: `docker compose logs inventory-sync`
2. Review API docs at: `http://localhost:8080/docs`
3. Create an issue in the repository
4. Contact the development team at Manzill Globe Trading
