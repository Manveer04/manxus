# Inventory Sync — Shopee · Lazada · TikTok Shop

Multi-platform inventory manager using browser automation.
Reads **and writes** stock across all 3 seller centers without needing
official API access.

---

## Quick Start (TrueNAS)

```bash
# Clone or copy this folder to your TrueNAS dataset
# e.g. /mnt/tank/docker/inventory-sync

cd inventory-sync
cp .env.example .env
# Fill .env with your real credentials before starting containers.
docker compose up -d
```

Open: **http://truenas-ip:8080**

This stack no longer includes Caddy. If you need HTTPS, terminate TLS at your own reverse proxy/load balancer and forward to the app container.

---

## First Time Login (Important)

Sessions are saved per-platform in the `./sessions/` volume.
The first time you run, there are no sessions — you need to log in manually.

**Option A — Log in via a separate headed browser session:**

```bash
# Run with headless=False temporarily to see the browser
docker compose exec inventory-sync python - <<'EOF'
import asyncio
from app.scrapers.shopee import ShopeeScraper

async def login():
    s = ShopeeScraper()
    await s.start(headless=False)  # shows browser window
    input("Log in manually, then press Enter...")
    await s.save_session()
    await s.close()
    print("Session saved!")

asyncio.run(login())
EOF
```

Repeat for `LazadaScraper` and `TikTokScraper`.

**Option B — Use Playwright's `codegen` to record your login:**

```bash
docker compose exec inventory-sync playwright codegen seller.shopee.com.my
```

---

## Fixing Selectors

Browser automation selectors break when platforms update their UI.
Here's how to find the correct ones:

### For any platform:
1. Open the seller center in Chrome
2. Open **DevTools → Network tab → filter "Fetch/XHR"**
3. Navigate to the product list
4. Look for API calls with "product", "item", or "sku" in the URL
5. Click the call → Preview → find `items` or `products` array in the JSON

### Finding stock input selectors:
1. Right-click the stock input field → **Inspect**
2. In the Elements panel, look for:
   - `data-testid` or `data-field` attributes (most stable)
   - `name` attribute on `<input>` elements
   - `aria-label` attributes
3. Update the selector in the relevant scraper file

### Quick selector test:
```bash
docker compose exec inventory-sync python - <<'EOF'
import asyncio
from app.scrapers.shopee import ShopeeScraper

async def test():
    s = ShopeeScraper()
    await s.start(headless=False)
    await s.page.goto("https://seller.shopee.com.my/portal/product/list/all")
    input("Inspect the page, then press Enter to close")
    await s.close()

asyncio.run(test())
EOF
```

---

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
