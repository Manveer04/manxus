import asyncio
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from playwright.async_api import BrowserContext, Page, async_playwright

PROJECT_ROOT = Path(__file__).resolve().parent
URLS_FILE = PROJECT_ROOT / "urls.txt"
COOKIES_FILE = PROJECT_ROOT / "shopee_cookies.json"
DB_FILE = PROJECT_ROOT / "invsync.db"


@dataclass
class ScrapeResult:
    url: str
    product_name: str | None
    original_price: float | None
    sale_price: float | None
    shipping_fee: float | None
    seller_name: str | None
    seller_location: str | None
    stock_quantity: int | None
    scraped_at: str


def read_urls(file_path: Path) -> list[str]:
    if not file_path.exists():
        return []
    urls: list[str] = []
    seen: set[str] = set()
    for line in file_path.read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        if not (value.startswith("http://") or value.startswith("https://")):
            continue
        if value in seen:
            continue
        seen.add(value)
        urls.append(value)
    return urls


def setup_db(db_path: Path) -> None:
    with sqlite3.connect(str(db_path)) as conn:
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


def insert_result(db_path: Path, result: ScrapeResult) -> None:
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """
            INSERT INTO price_history (
                url, product_name, original_price, sale_price, shipping_fee,
                seller_name, seller_location, stock_quantity, scraped_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result.url,
                result.product_name,
                result.original_price,
                result.sale_price,
                result.shipping_fee,
                result.seller_name,
                result.seller_location,
                result.stock_quantity,
                result.scraped_at,
            ),
        )
        conn.commit()


def _normalize_text(value: str | None) -> str | None:
    if value is None:
        return None
    compact = re.sub(r"\s+", " ", value).strip()
    return compact or None


def _parse_money(value: str | None) -> float | None:
    if not value:
        return None
    match = re.search(r"(\d{1,3}(?:[.,]\d{3})*(?:\.\d{1,2})|\d+(?:\.\d{1,2})?)", value.replace(",", ""))
    if not match:
        return None
    try:
        return float(match.group(1))
    except Exception:
        return None


def _parse_stock(value: str | None) -> int | None:
    if not value:
        return None
    match = re.search(r"(\d+)\s*(?:left|stocks?|pieces?)", value.lower())
    if not match:
        return None
    try:
        return int(match.group(1))
    except Exception:
        return None


def _normalize_cookie_entry(cookie: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(cookie, dict):
        return None

    name = cookie.get("name")
    value = cookie.get("value")
    if not name or value is None:
        return None

    normalized: dict[str, Any] = {
        "name": str(name),
        "value": str(value),
        "path": str(cookie.get("path") or "/"),
    }

    domain = cookie.get("domain")
    if domain:
        normalized["domain"] = str(domain)

    url = cookie.get("url")
    if url:
        normalized["url"] = str(url)

    expiration = cookie.get("expires", cookie.get("expirationDate"))
    if expiration is not None:
        try:
            normalized["expires"] = float(expiration)
        except Exception:
            pass

    same_site = cookie.get("sameSite")
    if isinstance(same_site, str) and same_site.lower() in {"lax", "strict", "none"}:
        normalized["sameSite"] = same_site.capitalize() if same_site.lower() != "none" else "None"

    if cookie.get("secure") is True:
        normalized["secure"] = True
    if cookie.get("httpOnly") is True:
        normalized["httpOnly"] = True

    return normalized


async def _load_cookies_if_any(context: BrowserContext) -> bool:
    if not COOKIES_FILE.exists():
        return False
    try:
        cookies = json.loads(COOKIES_FILE.read_text(encoding="utf-8"))
        if isinstance(cookies, list) and cookies:
            normalized = [entry for entry in (_normalize_cookie_entry(cookie) for cookie in cookies) if entry]
            if not normalized:
                return False
            await context.add_cookies(normalized)
            return True
    except Exception:
        return False
    return False


async def _save_cookies(context: BrowserContext) -> None:
    cookies = await context.cookies()
    COOKIES_FILE.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")


async def _is_login_page(page: Page) -> bool:
    url = page.url.lower()
    if "login" in url:
        return True
    selectors = [
        "input[name='loginKey']",
        "input[autocomplete='username']",
        "input[type='password']",
    ]
    for selector in selectors:
        if await page.locator(selector).count() > 0:
            return True
    return False


async def _extract_product_data(page: Page, url: str) -> ScrapeResult:
    await page.wait_for_load_state("domcontentloaded")
    await page.wait_for_timeout(2500)

    data: dict[str, Any] = await page.evaluate(
        r"""
() => {
    const txt = (el) => (el ? el.textContent.replace(/\s+/g, ' ').trim() : null);
    const attr = (selector, name) => {
        const el = document.querySelector(selector);
        return el ? el.getAttribute(name) : null;
    };
    const allMeta = Array.from(document.querySelectorAll('meta')).map((m) => ({
        name: m.getAttribute('name'),
        property: m.getAttribute('property'),
        content: m.getAttribute('content'),
    }));
    const jsonld = Array.from(document.querySelectorAll('script[type="application/ld+json"]')).map((el) => el.textContent || '').filter(Boolean);
    const bodyText = document.body ? document.body.innerText : '';
    return {
        title: document.title || null,
        h1: txt(document.querySelector('h1')),
        bodyText,
        meta: allMeta,
        jsonld,
        ogTitle: attr('meta[property="og:title"]', 'content'),
        ogDescription: attr('meta[property="og:description"]', 'content'),
        ogPrice: attr('meta[property="og:price:amount"]', 'content') || attr('meta[property="product:price:amount"]', 'content'),
        ogCurrency: attr('meta[property="og:price:currency"]', 'content') || attr('meta[property="product:price:currency"]', 'content'),
        canonical: attr('link[rel="canonical"]', 'href'),
    };
}
                """
    )

    def _first_non_empty(*values: str | None) -> str | None:
        for value in values:
            value = _normalize_text(value)
            if value:
                return value
        return None

    def _extract_jsonld_product(payloads: list[str]) -> dict[str, Any]:
        for raw in payloads:
            try:
                parsed = json.loads(raw)
            except Exception:
                continue
            items = parsed if isinstance(parsed, list) else [parsed]
            for item in items:
                if not isinstance(item, dict):
                    continue
                if str(item.get("@type") or "").lower() == "product":
                    return item
                graph = item.get("@graph") if isinstance(item.get("@graph"), list) else []
                for graph_item in graph:
                    if isinstance(graph_item, dict) and str(graph_item.get("@type") or "").lower() == "product":
                        return graph_item
        return {}

    product_ld = _extract_jsonld_product(data.get("jsonld", []))
    offer_ld = product_ld.get("offers") if isinstance(product_ld.get("offers"), dict) else {}

    body_text = data.get("bodyText") or ""
    price_matches = [m.group(0) for m in re.finditer(r"RM\s*\d+(?:\.\d{1,2})?", body_text)]
    numeric_price_matches = [_parse_money(v) for v in price_matches]
    numeric_price_matches = [v for v in numeric_price_matches if v is not None]

    product_name = _first_non_empty(
        data.get("h1"),
        data.get("ogTitle"),
        product_ld.get("name") if isinstance(product_ld.get("name"), str) else None,
        data.get("title"),
    )

    seller_name = _first_non_empty(
        (offer_ld.get("seller") or {}).get("name") if isinstance(offer_ld.get("seller"), dict) else None,
        product_ld.get("brand", {}).get("name") if isinstance(product_ld.get("brand"), dict) else None,
        None,
    )

    seller_location = _first_non_empty(
        data.get("ogDescription"),
        next((line.strip() for line in body_text.split("\n") if re.search(r"\b(from|dari)\b", line, re.I)), None),
    )

    shipping_text = _first_non_empty(
        next((line.strip() for line in body_text.split("\n") if re.search(r"shipping|penghantaran|delivery", line, re.I)), None),
    )

    stock_text = next((line.strip() for line in body_text.split("\n") if re.search(r"\d+\s*(left|stock|stocks|pieces)", line, re.I)), None)

    original_price = _parse_money(str(offer_ld.get("highPrice"))) if offer_ld.get("highPrice") is not None else None
    sale_price = _parse_money(str(offer_ld.get("price"))) if offer_ld.get("price") is not None else None

    if sale_price is None and data.get("ogPrice"):
        sale_price = _parse_money(str(data.get("ogPrice")))

    if sale_price is None and numeric_price_matches:
        sale_price = min(numeric_price_matches)
    if original_price is None and len(numeric_price_matches) >= 2:
        original_price = max(numeric_price_matches)

    if original_price is not None and sale_price is not None and original_price < sale_price:
        original_price, sale_price = sale_price, original_price

    return ScrapeResult(
        url=url,
        product_name=product_name,
        original_price=original_price,
        sale_price=sale_price,
        shipping_fee=_parse_money(shipping_text),
        seller_name=seller_name,
        seller_location=seller_location,
        stock_quantity=_parse_stock(stock_text),
        scraped_at=datetime.now(timezone.utc).isoformat(),
    )


async def scrape_urls(urls: list[str]) -> tuple[list[ScrapeResult], list[tuple[str, str]]]:
    results: list[ScrapeResult] = []
    failures: list[tuple[str, str]] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(locale="en-MY", timezone_id="Asia/Kuala_Lumpur")
        await _load_cookies_if_any(context)
        page = await context.new_page()

        logged_in_once = False

        for url in urls:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)

                if await _is_login_page(page):
                    raise RuntimeError("Shopee returned a login page; the saved cookies are missing, expired, or invalid")

                result = await _extract_product_data(page, url)
                results.append(result)
            except Exception as exc:
                failures.append((url, str(exc)))

        await browser.close()

    return results, failures


def print_summary(results: list[ScrapeResult], failures: list[tuple[str, str]], started_at: datetime) -> None:
    completed_at = datetime.now(timezone.utc)
    elapsed = (completed_at - started_at).total_seconds()

    print("\nShopee Tracker Summary")
    print("=" * 72)
    print(f"Started (UTC):   {started_at.isoformat()}")
    print(f"Completed (UTC): {completed_at.isoformat()}")
    print(f"Elapsed:         {elapsed:.1f}s")
    print(f"Success:         {len(results)}")
    print(f"Failed:          {len(failures)}")

    if results:
        print("\nLatest captures:")
        for row in results:
            active_price = row.sale_price if row.sale_price is not None else row.original_price
            print(
                f"- {row.product_name or 'Unknown Product'} | "
                f"Price: {active_price if active_price is not None else 'N/A'} | "
                f"Seller: {row.seller_name or 'N/A'}"
            )

    if failures:
        print("\nFailures:")
        for failed_url, reason in failures:
            print(f"- {failed_url} -> {reason}")


async def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")

    setup_db(DB_FILE)
    urls = read_urls(URLS_FILE)
    if not urls:
        print(f"No URLs found in {URLS_FILE}")
        return 1

    started_at = datetime.now(timezone.utc)
    results, failures = await scrape_urls(urls)

    for result in results:
        insert_result(DB_FILE, result)

    print_summary(results, failures, started_at)
    return 0 if results else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
