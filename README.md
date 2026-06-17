<div align="center">
  <img src="Manxus_Logo.png" alt="Manxus Logo" width="25%">

  # Manxus

  Internal Operations Platform for Manzill Globe Trading
</div>

---

## Overview

Manxus is a FastAPI-based operations backend for inventory, orders, documents, and finance. This repository documents and ships the core application only. Legacy browser scraping is unsupported and removed from the supported deployment path because it was unreliable and created compliance risk.

## Tech Stack

FastAPI, SQLAlchemy, APScheduler, HTTPx, OpenPyXL, SQLite.

## Configuration

Copy `.env.example` to `.env` and fill in the API/token credentials you actually use.

Key settings:

- `APP_BASIC_AUTH_USER`
- `APP_BASIC_AUTH_PASSWORD`
- `REQUIRE_API_AUTH`
- `ALLOW_REMOTE_DATABASE`
- `SHOPEE_APP_KEY`
- `SHOPEE_APP_SECRET`
- `SHOPEE_ACCESS_TOKEN`
- `SHOPEE_REFRESH_TOKEN`
- `LAZADA_APP_KEY`
- `LAZADA_APP_SECRET`
- `LAZADA_AUTH_CODE`
- `TIKTOK_APP_KEY`
- `TIKTOK_APP_SECRET`
- `TIKTOK_AUTH_CODE`

## Run

```bash
docker compose up -d
```

The app listens on port `8080` via Docker Compose.

## API Surface

- Inventory and product management under `/api`
- Order storage and notification tools under `/api`
- Financial and document routes under `/api/financials`
- Health, auth, and static UI routes in `app/main.py`

Unsupported marketplace operations return a clear 503 response telling you the API/token integration is not configured in this build.

## Deployment

- Docker is the supported deployment path.
- Kubernetes manifests are not bundled.
- Legacy browser-scraping scripts, session helpers, and resolver tooling are removed.

## Notes

The repository intentionally avoids browser-login/session workflows. If a marketplace action is unavailable, configure the explicit API/token integration or treat the feature as partial/not bundled.
