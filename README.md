# Manxus

**Internal Operations Platform for Manzill Globe Trading**

Manxus is a comprehensive full-stack operations platform designed to streamline the daily operations of Manzill Globe Trading, a Malaysian online grocery retailer operating across Shopee, Lazada, and TikTok Shop. 

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

## License

[Your License Here]

---

## Support & Contributions

For questions, bugs, or feature requests:
1. Check logs: `docker compose logs inventory-sync`
2. Review API docs at: `http://localhost:8080/docs`
3. Create an issue in the repository
4. Contact the development team at Manzill Globe Trading

## Configuration

### Environment Variables

**Core Database & Server**
```env
DATABASE_URL=sqlite:////app/db/inventory.db
PUBLIC_BASE_URL=http://localhost:8080
ORDER_ACTION_BASE_URL=http://inventory-sync:8000
```

**Sync Scheduling**
```env
SYNC_INTERVAL_MINUTES=30              # How often to pull inventory
ORDER_INTERVAL_MINUTES=10             # How often to check for new orders
NOTIFY_RETRY_INTERVAL_MINUTES=5       # Retry failed notifications
AUTO_SYNC_ENABLED=true                # Enable automatic sync
ORDER_SYNC_ON_STARTUP=true            # Sync orders on app start
```

**Shopee Configuration**
```env
SHOPEE_APP_KEY=<your-key>
SHOPEE_APP_SECRET=<your-secret>
SHOPEE_MAIN_SHOP_ID=<shop-id>
SHOPEE_SG_SHOP_ID=<singapore-shop-id>
SHOPEE_ACCESS_TOKEN=<token>           # Auto-refreshed if blank
SHOPEE_REFRESH_TOKEN=<refresh-token>
```

**Lazada Configuration**
```env
LAZADA_APP_KEY=<your-key>
LAZADA_APP_SECRET=<your-secret>
LAZADA_AUTH_CODE=<code>               # Exchange for access token on first run
```

**TikTok Shop Configuration**
```env
TIKTOK_APP_KEY=<your-key>
TIKTOK_APP_SECRET=<your-secret>
TIKTOK_AUTH_CODE=<code>               # Exchange for access token on first run
```

**Email & Notifications**
```env
YAHOO_EMAIL=your-email@yahoo.com
YAHOO_APP_PASSWORD=<app-password>     # Yahoo app-specific password
NTFY_TOPIC=<topic>                    # Optional: ntfy.sh topic for alerts
```

**Security**
```env
APP_BASIC_AUTH_USER=admin
APP_BASIC_AUTH_PASSWORD=your-password
REQUIRE_API_AUTH=true
TOKEN_ENCRYPTION_KEY=<random-string>
APP_OAUTH_STATE_SECRET=<random-string>
ENFORCE_HTTPS=false                   # Enable in production
```

**Advanced**
```env
COGS_LIFO_SWITCH_DATE=2024-01-01      # Switch from FIFO to LIFO on this date
ALLOW_REMOTE_DATABASE=false           # Security: prevent remote DB connections
TZ=Asia/Kuala_Lumpur                  # Timezone for timestamps
```

---

## API Endpoints

### Inventory Management
- `GET /api/products` — List all products
- `POST /api/products` — Create product
- `PUT /api/products/{id}` — Update product
- `GET /api/products/{id}/listings` — Get platform listings
- `POST /api/sync/pull` — Force pull from all platforms
- `POST /api/sync/push/{product_id}` — Force push to all platforms
- `GET /api/sync/logs` — View sync history

### Orders
- `GET /api/orders` — List all orders
- `GET /api/orders/{id}` — Get order details
- `POST /api/orders/{id}/shipment` — Arrange shipment

### Financial
- `GET /api/financials/transactions` — View financial records
- `POST /api/financials/invoices` — Create invoice
- `POST /api/financials/purchase-orders` — Create PO
- `GET /api/financials/cogs` — Calculate COGS

### Auth
- `POST /api/auth/shopee/callback` — Shopee OAuth callback
- `POST /api/auth/lazada/callback` — Lazada OAuth callback
- `POST /api/device-token` — Register device for push notifications

---

## Project Structure

```
.
├── app/
│   ├── main.py                    # FastAPI app entry point
│   ├── models.py                  # SQLAlchemy database models
│   ├── database.py                # DB connection & session
│   ├── sync_engine.py             # Core sync logic (pull/push/detect)
│   ├── order_engine.py            # Order processing & shipment
│   ├── scheduler.py               # APScheduler background jobs
│   ├── email_watcher.py           # Gmail/Yahoo email monitoring
│   ├── notifier.py                # Push notification logic
│   ├── security_utils.py          # Auth & encryption utilities
│   ├── api/
│   │   └── routes.py              # Main REST API endpoints
│   ├── financials/
│   │   ├── models.py              # Financial data models
│   │   ├── routes.py              # Financial API endpoints
│   │   ├── document_routes.py     # Invoice/PO endpoints
│   │   └── parsers.py             # Receipt/invoice parsers
│   ├── static/                    # Frontend HTML/JS/CSS
│   │   ├── index.html             # Dashboard
│   │   ├── orders_admin.html      # Order management
│   │   ├── purchases.html         # Financial tracking
│   │   └── ...
│   └── *_auth.py                  # OAuth handlers (shopee, lazada)
├── data/
│   ├── purchases/                 # Receipt photos & batch data
│   └── inspect_*.py               # Debug utilities
├── db/
│   └── migrations/                # Database migration scripts
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
└── README.md
```

### Core Modules Explained

**`sync_engine.py`**  
Orchestrates the entire sync flow:
1. Pulls stock from all platforms
2. Compares against master stock in DB
3. Detects sales (stock decreases)
4. Deducts from grouped products
5. Pushes updates back to platforms
6. Logs all actions

**`order_engine.py`**  
Manages order lifecycle:
- Fetch new orders from seller centers
- Parse order details and shipping info
- Generate shipping manifests
- Update order status and tracking

**`scheduler.py`**  
Background tasks via APScheduler:
- Auto-pull inventory (every N minutes)
- Auto-push out-of-sync products
- Refresh OAuth tokens
- Notify unshipped orders
- Retry failed notifications

**`financials/`**  
Financial tracking module:
- Track product costs and COGS
- Generate invoices and POs
- Parse supplier receipts
- Manage packaging suppliers
- Off-platform sales tracking

---

## Database Models

### Inventory
- **ProductGroup** — Logical grouping of products (one master stock)
- **Product** — Individual SKU with master stock level
- **PlatformListing** — Each product's presence on a platform
- **SyncLog** — Complete history of every sync action

### Orders
- **Order** — Marketplace order with buyer info
- **OrderItem** — Line items in an order
- **Shipment** — Shipping manifest & AWB tracking

### Financial
- **FinancialTransaction** — Ledger entries (income/expenses)
- **PurchaseBatch** — Supplier purchase orders with photos
- **GeneratedInvoice** — Generated invoices to customers
- **GeneratedPurchaseOrder** — Generated POs to suppliers
- **PackagingSupplier** — Packaging material suppliers

---

## Troubleshooting

### Browser Automation Issues

**Problem:** "Selector not found" errors during sync

**Solution:** UI changes require selector updates. Find the correct selector:

1. Open seller center in your browser
2. Open DevTools → Network tab → filter "XHR"
3. Navigate to the product list
4. Look for API calls with "product" or "item" in URL
5. Check the JSON response to understand the data structure
6. Update the selector in the relevant scraper file

**Example: Finding Shopee stock input**
```
1. Right-click stock input → Inspect
2. Look for: data-testid, name, or aria-label
3. Update app/scrapers/shopee.py selector to match
4. Test with: docker compose exec inventory-sync python -c "..."
```

### Session Expired

**Problem:** "Not logged in" errors

**Solution:** Re-run the login procedure to generate fresh cookies

```bash
docker compose exec inventory-sync python - <<'EOF'
from app.scrapers import SCRAPERS
# ... run login for each platform ...
EOF
```

### Sync Stopped Working

**Problem:** Inventory not pulling/pushing

**Checks:**
1. Check logs: `docker compose logs inventory-sync`
2. Verify credentials in `.env` file
3. Check if browser session is still valid
4. Manually trigger sync via API: `curl -X POST http://localhost:8080/api/sync/pull`

### Database Issues

**Problem:** "Database is locked" errors

**Solution:** Restart the app
```bash
docker compose restart inventory-sync
```

---

## Development

### Local Development (Without Docker)

```bash
# Create virtual environment
python3.11 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Set environment variables
export DATABASE_URL=sqlite:///inventory.db
export PUBLIC_BASE_URL=http://localhost:8000

# Run the server
python -m uvicorn app.main:app --reload
```

### Running Tests

```bash
# Run sync manually
python -c "
import asyncio
from app.sync_engine import SyncEngine
from app.database import SessionLocal

async def test():
    db = SessionLocal()
    engine = SyncEngine()
    results = await engine.pull_all(db)
    print(results)
    db.close()

asyncio.run(test())
"
```

### Database Migrations

Alembic is configured for SQLite. Create new migrations:

```bash
docker compose exec inventory-sync alembic revision --autogenerate -m "description"
docker compose exec inventory-sync alembic upgrade head
```

---

## Performance Tips

- **Sync Interval**: Set `SYNC_INTERVAL_MINUTES` to 30-60 for production (more frequent = higher API usage)
- **Order Sync**: Set `ORDER_INTERVAL_MINUTES` to 5-10 for real-time order updates
- **Database**: SQLite is fine for most use cases; migrate to PostgreSQL for multi-instance deployments
- **Memory**: App uses ~500MB with `shm_size: "2gb"` in Docker for Playwright
- **Timezone**: Keep `TZ` consistent across all instances

---

## License

[Your License Here]

---

## Support

For issues, questions, or contributions:
1. Check the troubleshooting section above
2. Review sync logs in the dashboard
3. Check Docker logs: `docker compose logs inventory-sync`
4. Create an issue in the repository

## Architecture

```
inventory-sync/
├── app/
│   ├── main.py          — FastAPI entrypoint
│   ├── database.py      — SQLite connection
│   ├── models.py        — Product, PlatformListing, SyncLog tables
│   ├── sync_engine.py   — Core read/write/sync logic
│   ├── scheduler.py     — Auto-sync every N minutes
│   ├── scrapers/
│   │   ├── base.py      — Session management, stealth, delays
│   │   ├── shopee.py    — Shopee Seller Center
│   │   ├── lazada.py    — Lazada Seller Center (+ network intercept)
│   │   └── tiktok.py    — TikTok Shop (+ network intercept)
│   ├── api/
│   │   └── routes.py    — REST API endpoints
│   └── static/
│       └── index.html   — Dashboard UI
├── sessions/            — Persisted browser sessions (gitignore this!)
├── db/                  — SQLite database file
├── docker-compose.yml
├── Dockerfile
└── requirements.txt
```

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET    | `/api/products` | List all products with platform listings |
| PATCH  | `/api/products/{id}` | Update master stock (marks out-of-sync) |
| POST   | `/api/products/{id}/push` | Push new stock to one/all platforms |
| POST   | `/api/sync/pull-all` | Pull fresh stock from all platforms |
| POST   | `/api/sync/push-out-of-sync` | Push master to all out-of-sync listings |
| GET    | `/api/platforms/status` | Session status per platform |
| DELETE | `/api/platforms/{platform}/session` | Force re-login |
| GET    | `/api/logs` | Recent sync activity log |
| GET    | `/api/stats` | Dashboard stats |

### Shopee Shipment / AWB (new)

Use stored order `id` from `/api/orders`.

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/orders/shopee/{order_id}/arrange-shipment` | Call Shopee `ship_order` to arrange shipment |
| POST | `/api/orders/shopee/{order_id}/awb/create` | Create AWB (shipping document) |
| GET  | `/api/orders/shopee/{order_id}/awb` | Check AWB generation result/status |

Examples:

```bash
# Arrange shipment
curl -X POST "http://localhost:8080/api/orders/shopee/123/arrange-shipment" \
  -H "Content-Type: application/json" \
  -d '{"package_number":""}'

# Create AWB
curl -X POST "http://localhost:8080/api/orders/shopee/123/awb/create" \
  -H "Content-Type: application/json" \
  -d '{"package_number":"","shipping_document_type":""}'

# Check AWB result
curl "http://localhost:8080/api/orders/shopee/123/awb"
```

### Lazada Shipment / AWB (new)

Use stored order `id` from `/api/orders?platform=lazada`.

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/orders/lazada/{order_id}/arrange-shipment` | Attempt to set Lazada order to ready-to-ship via supported endpoint paths |
| POST | `/api/orders/lazada/{order_id}/awb/create` | Attempt to create/fetch Lazada AWB and poll briefly for print URL |
| GET  | `/api/orders/lazada/{order_id}/awb` | Fetch latest Lazada AWB result and normalized print URL |

Examples:

```bash
# Arrange shipment
curl -X POST "http://localhost:8080/api/orders/lazada/123/arrange-shipment"

# Create AWB (with polling)
curl -X POST "http://localhost:8080/api/orders/lazada/123/awb/create" \
  -H "Content-Type: application/json" \
  -d '{"wait_seconds":25,"poll_seconds":2}'

# Check AWB result
curl "http://localhost:8080/api/orders/lazada/123/awb"
```

### TikTok Shipment / AWB (new)

Use stored order `id` from `/api/orders?platform=tiktok`.

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/orders/tiktok/{order_id}/arrange-shipment` | Attempt TikTok shipment arrangement |
| POST | `/api/orders/tiktok/{order_id}/awb/create` | Attempt AWB creation and poll briefly for print URL |
| GET  | `/api/orders/tiktok/{order_id}/awb` | Fetch latest TikTok AWB result and normalized print URL |

Examples:

```bash
# Arrange shipment
curl -X POST "http://localhost:8080/api/orders/tiktok/123/arrange-shipment" \
  -H "Content-Type: application/json" \
  -d '{"package_id":""}'

# Create AWB (with polling)
curl -X POST "http://localhost:8080/api/orders/tiktok/123/awb/create" \
  -H "Content-Type: application/json" \
  -d '{"package_id":"","wait_seconds":25,"poll_seconds":2}'

# Check AWB result
curl "http://localhost:8080/api/orders/tiktok/123/awb"
```

### YT Resolver

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/yt/health` | Basic health check |
| POST | `/api/yt/extract` | Resolve a YouTube page URL into a direct stream URL |

Example:

```bash
curl -X POST "http://localhost:8080/api/yt/extract" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://www.youtube.com/watch?v=8pv0mah8TeU"}'
```

If you set `YT_RESOLVER_TOKEN`, send `Authorization: Bearer <token>` with the request.

---

## Environment Variables

```env
SYNC_INTERVAL_MINUTES=30            # How often to auto-sync inventory
ORDER_INTERVAL_MINUTES=10           # How often to fetch new orders
ORDER_SYNC_ON_STARTUP=true          # Run order fetch once shortly after startup
ORDER_SYNC_STARTUP_DELAY_SECONDS=20 # Delay before startup order fetch
AUTO_SYNC_ENABLED=true              # Set to false to disable background jobs
DATABASE_URL=sqlite:////app/db/inventory.db
APP_BASIC_AUTH_USER=admin           # optional but strongly recommended
APP_BASIC_AUTH_PASSWORD=change-me   # optional but strongly recommended
REQUIRE_API_AUTH=true               # deny /api requests if auth creds are not configured
ALLOW_REMOTE_DATABASE=false         # keep remote DB URLs disabled unless explicitly trusted
LOCAL_HOSTNAME=192.168.50.129       # LAN hostname or IP used by the proxy
TOKEN_ENCRYPTION_KEY=change-me      # recommended: long random secret for token file encryption at rest
APP_OAUTH_STATE_SECRET=change-me    # optional dedicated OAuth state signing secret
ENFORCE_HTTPS=false                 # set true in production behind HTTPS reverse proxy
ALLOW_INSECURE_REDIRECT_URI=false   # keep false in production; true only for local non-HTTPS testing
```

## Security Notes

- Never store real secrets in `docker-compose.yml`, source files, or static frontend code.
- Keep secrets in `.env` (ignored by git) and rotate any credentials that were previously committed.
- Session/token files under `sessions/` and `/app/data/shopee_tokens` are sensitive and should not be committed.
- Lazada/Shopee token files are encrypted at rest. Set `TOKEN_ENCRYPTION_KEY` for consistent key management.
- If `APP_BASIC_AUTH_USER` and `APP_BASIC_AUTH_PASSWORD` are set, the app enforces HTTP Basic authentication for all routes (except Shopee OAuth callback).
- With `REQUIRE_API_AUTH=true` (default), `/api/*` requests are rejected unless Basic Auth credentials are configured.
- The app refuses remote `DATABASE_URL` hosts unless `ALLOW_REMOTE_DATABASE=true` is set explicitly.
- Browser Basic Auth credentials are stored in `sessionStorage`, not `localStorage`, so they clear when the browser session ends.
- Open the app on `http://<lan-host>:8080` (or through your own HTTPS reverse proxy).
- The VNC desktop is available at `http://<lan-host>:8080/vnc/`.

---

## Notes & Limitations

- **Session expiry**: Platform sessions typically last 7–30 days.
  You'll get an alert in the Activity Log when re-login is needed.
- **Variation products**: Each SKU variation has its own stock. The
  scrapers update all variations to the same value — modify if needed.
- **TikTok bot detection**: TikTok is the most aggressive. If scrapes
  fail, increase delay values in `tiktok.py` or reduce sync frequency.
- **Lazada preferred approach**: Uses network response interception
  rather than HTML scraping — more reliable long-term.
