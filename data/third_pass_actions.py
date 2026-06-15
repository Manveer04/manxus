import asyncio
import json
from typing import Any, Dict, List

import httpx

from app.scrapers.lazada import LazadaScraper
from app.scrapers.tiktok import TikTokScraper

BASE = "http://127.0.0.1:8080"


def api_get(path: str) -> Dict[str, Any]:
    url = f"{BASE}{path}"
    out: Dict[str, Any] = {"method": "GET", "url": url, "sent": None}
    try:
        with httpx.Client(timeout=180.0) as client:
            resp = client.get(url)
        out["http_status"] = resp.status_code
        try:
            out["response"] = resp.json()
        except Exception:
            out["response"] = resp.text
        out["ok"] = resp.status_code < 400
    except Exception as e:
        out["ok"] = False
        out["error"] = str(e)
    return out


def api_post(path: str, body: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{BASE}{path}"
    out: Dict[str, Any] = {"method": "POST", "url": url, "sent": body}
    try:
        with httpx.Client(timeout=240.0) as client:
            resp = client.post(url, json=body)
        out["http_status"] = resp.status_code
        try:
            out["response"] = resp.json()
        except Exception:
            out["response"] = resp.text
        out["ok"] = resp.status_code < 400
    except Exception as e:
        out["ok"] = False
        out["error"] = str(e)
    return out


def _extract_nested_strings(v: Any) -> List[str]:
    out: List[str] = []
    if isinstance(v, dict):
        for vv in v.values():
            out.extend(_extract_nested_strings(vv))
    elif isinstance(v, list):
        for vv in v:
            out.extend(_extract_nested_strings(vv))
    elif isinstance(v, str):
        out.append(v)
    return out


async def lazada_endpoint_discovery() -> Dict[str, Any]:
    scraper = LazadaScraper()
    await scraper.start()
    try:
        candidates = [
            "/api/sdk/endpoint/list",
            "/sdk/endpoint/list",
            "/seller/endpoint/list",
            "/app/endpoint/list",
            "/api/endpoint/list",
        ]
        attempts = []
        for p in candidates:
            try:
                data = await scraper._request(p, "GET", {})
                attempts.append({"path": p, "response": data})
            except Exception as e:
                attempts.append({"path": p, "error": str(e)})

        # Find any successful payload and search for target terms.
        terms = ("pack", "status", "ready_to_ship", "readytoship", "packed")
        matches = []
        for att in attempts:
            payload = att.get("response")
            if not isinstance(payload, dict):
                continue
            text = "\n".join(_extract_nested_strings(payload)).lower()
            for t in terms:
                if t in text:
                    matches.append({"path": att.get("path"), "term": t})

        return {
            "attempts": attempts,
            "matches": matches,
        }
    finally:
        await scraper.close()


async def find_tiktok_shippable(orders: List[Dict[str, Any]]) -> Dict[str, Any]:
    scraper = TikTokScraper()
    await scraper.start()
    try:
        shippable_statuses = {
            "AWAITING_SHIPMENT",
            "AWAITING_COLLECTION",
            "TO_SHIP",
            "READY_TO_SHIP",
            "UNSHIPPED",
            "WAIT_SELLER_SEND_GOODS",
        }
        inspected = []
        selected = None

        for o in orders:
            if o.get("platform") != "tiktok":
                continue
            detail = await scraper._get_order_detail_raw(str(o.get("platform_order_id")))
            orders_list = ((detail.get("data") or {}).get("orders") or [])
            if not orders_list:
                inspected.append({"order_id": o.get("id"), "platform_order_id": o.get("platform_order_id"), "found": False})
                continue

            od = orders_list[0]
            order_status = str(od.get("status") or "").upper()
            package_ids = scraper._extract_package_ids_from_order_detail(detail)
            package_statuses = set()
            for li in (od.get("line_items") or []):
                ps = str(li.get("package_status") or "").upper()
                if ps:
                    package_statuses.add(ps)

            item = {
                "order_id": o.get("id"),
                "platform_order_id": o.get("platform_order_id"),
                "order_status": order_status,
                "package_statuses": sorted(package_statuses),
                "package_ids": package_ids,
                "raw_detail": detail,
            }
            inspected.append(item)

            status_union = set([order_status]) | package_statuses
            if (status_union & shippable_statuses) and package_ids:
                selected = item
                break

        return {"inspected": inspected, "selected": selected, "shippable_statuses": sorted(shippable_statuses)}
    finally:
        await scraper.close()


async def main():
    result: Dict[str, Any] = {}

    # Load current orders
    orders_call = api_get("/api/orders?limit=1000")
    result["orders_call"] = orders_call
    orders = orders_call.get("response") or []

    # 1) Shopee: find current READY_TO_SHIP and run AWB chain only
    shopee_candidates = [
        o for o in orders
        if str(o.get("platform")) in ("shopee", "shopee_sg") and str(o.get("status", "")).lower() == "ready_to_ship"
    ]

    shopee_out: Dict[str, Any] = {
        "candidates": [{"id": o.get("id"), "platform_order_id": o.get("platform_order_id"), "status": o.get("status")} for o in shopee_candidates],
        "selected": None,
        "steps": [],
    }

    if shopee_candidates:
        selected = shopee_candidates[0]
        shopee_out["selected"] = {"id": selected.get("id"), "platform_order_id": selected.get("platform_order_id"), "status": selected.get("status")}
        awb = api_post(
            f"/api/orders/shopee/{selected.get('id')}/awb/create",
            {"package_number": "", "shipping_document_type": "", "wait_seconds": 120, "poll_seconds": 3},
        )
        shopee_out["steps"].append({
            "name": "awb_chain_only_create_poll_download",
            "call": awb,
            "next": "done" if awb.get("ok") else "blocked",
        })
    else:
        shopee_out["note"] = "No Shopee order currently in ready_to_ship status in DB"

    result["shopee_action_1"] = shopee_out

    # 2) Lazada endpoint discovery
    result["lazada_action_2"] = await lazada_endpoint_discovery()

    # 3) TikTok: find shippable order/package and run full chain
    tt_probe = await find_tiktok_shippable(orders)
    tt_out: Dict[str, Any] = {
        "probe": tt_probe,
        "steps": [],
    }

    selected_tt = tt_probe.get("selected")
    if selected_tt:
        internal_order_id = selected_tt.get("order_id")
        package_id = (selected_tt.get("package_ids") or [""])[0]

        arr = api_post(f"/api/orders/tiktok/{internal_order_id}/arrange-shipment", {"package_id": package_id})

        semantic_failed = False
        resp = arr.get("response")
        try:
            errs = (((resp or {}).get("response") or {}).get("data") or {}).get("errors") or []
            semantic_failed = len(errs) > 0
        except Exception:
            semantic_failed = False

        tt_out["steps"].append({
            "name": "arrange_shipment_ship_package",
            "call": arr,
            "semantic_failed": semantic_failed,
            "next": "blocked" if (not arr.get("ok") or semantic_failed) else "awb",
        })

        if arr.get("ok") and (not semantic_failed):
            awb = api_post(
                f"/api/orders/tiktok/{internal_order_id}/awb/create",
                {"package_id": package_id, "wait_seconds": 25, "poll_seconds": 2},
            )
            tt_out["steps"].append({
                "name": "shipping_document",
                "call": awb,
                "next": "done" if awb.get("ok") else "blocked",
            })
    else:
        tt_out["note"] = "No TikTok order/package currently in a shippable status"

    result["tiktok_action_3"] = tt_out

    print(json.dumps(result, indent=2, ensure_ascii=True))


if __name__ == "__main__":
    asyncio.run(main())
