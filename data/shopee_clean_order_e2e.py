import json
import time
from datetime import datetime, timedelta, timezone

from app.scrapers.shopee_api import ShopeeAPIClient

SHOP_ID = 411435241
DOC_TYPE = "THERMAL_AIR_WAYBILL"
POLL_SECONDS = 5
POLL_TIMEOUT_SECONDS = 300
POST_SHIP_WAIT_SECONDS = 180


def _extract_first_pickup(preflight: dict):
    resp = preflight.get("response") or {}
    pickup = resp.get("pickup") or {}
    addresses = pickup.get("address_list") or []
    for addr in addresses:
        address_id = addr.get("address_id")
        slots = addr.get("time_slot_list") or []
        for slot in slots:
            pickup_time_id = slot.get("pickup_time_id")
            if address_id and pickup_time_id:
                return int(address_id), str(pickup_time_id)
    return None, None


def _first_order(detail: dict):
    order_list = ((detail.get("response") or {}).get("order_list") or [])
    return (order_list[0] if order_list else {}) or {}


def _find_clean_ready_to_ship_order(client: ShopeeAPIClient):
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=3)

    cursor = ""
    while True:
        resp = client.get_order_list(
            time_from=int(start.timestamp()),
            time_to=int(now.timestamp()),
            order_status="READY_TO_SHIP",
            cursor=cursor,
            page_size=50,
        )

        rows = ((resp.get("response") or {}).get("order_list") or [])
        rows = sorted(rows, key=lambda x: int(x.get("create_time") or 0), reverse=True)

        for row in rows:
            order_sn = str(row.get("order_sn") or "").strip()
            if not order_sn:
                continue

            detail = client.get_order_detail([order_sn])
            first = _first_order(detail)
            shipping_carrier = str(first.get("shipping_carrier") or "").strip()
            order_status = str(first.get("order_status") or "").strip().upper()

            if order_status != "READY_TO_SHIP":
                continue
            if shipping_carrier:
                continue

            return {
                "order_sn": order_sn,
                "order_status": order_status,
                "shipping_carrier": shipping_carrier,
                "create_time": row.get("create_time"),
                "detail": detail,
            }

        more = bool((resp.get("response") or {}).get("more"))
        cursor = str((resp.get("response") or {}).get("next_cursor") or "")
        if not more or not cursor:
            break

    return None


def main():
    client = ShopeeAPIClient(SHOP_ID)
    out = {
        "shop_id": SHOP_ID,
        "doc_type": DOC_TYPE,
        "steps": {},
    }

    clean = _find_clean_ready_to_ship_order(client)
    if not clean:
        out["steps"]["find_clean_order"] = {
            "ok": False,
            "reason": "no_ready_to_ship_order_with_empty_shipping_carrier",
        }
        print(json.dumps(out, indent=2, ensure_ascii=True))
        return

    order_sn = clean["order_sn"]
    out["order_sn"] = order_sn
    out["steps"]["find_clean_order"] = {
        "ok": True,
        "order_sn": order_sn,
        "order_status": clean.get("order_status"),
        "shipping_carrier": clean.get("shipping_carrier"),
        "create_time": clean.get("create_time"),
    }

    preflight = client.get_shipping_parameter(order_sn)
    address_id, pickup_time_id = _extract_first_pickup(preflight)
    out["steps"]["get_shipping_parameter"] = {
        "response": preflight,
        "address_id": address_id,
        "pickup_time_id": pickup_time_id,
    }
    if not address_id or not pickup_time_id:
        out["steps"]["ship_order"] = {
            "ok": False,
            "reason": "no_pickup_slot_from_preflight",
        }
        print(json.dumps(out, indent=2, ensure_ascii=True))
        return

    ship_req = {
        "order_sn": order_sn,
        "pickup": {
            "address_id": address_id,
            "pickup_time_id": pickup_time_id,
        },
    }
    ship_resp = client.ship_order(
        order_sn=order_sn,
        package_number="",
        pickup=ship_req["pickup"],
        dropoff=None,
        non_integrated=None,
    )
    out["steps"]["ship_order"] = {
        "request": ship_req,
        "response": ship_resp,
        "called_exactly_once": True,
    }

    time.sleep(POST_SHIP_WAIT_SECONDS)

    detail_after_ship = client.get_order_detail([order_sn])
    first_after = _first_order(detail_after_ship)
    package_list = first_after.get("package_list") or []
    package_number = ""
    if package_list:
        package_number = str((package_list[0] or {}).get("package_number") or "").strip()

    out["steps"]["get_order_detail_after_ship"] = {
        "response": detail_after_ship,
        "package_number": package_number,
        "shipping_carrier": first_after.get("shipping_carrier"),
        "order_status": first_after.get("order_status"),
    }

    create_req = {
        "order_list": [
            {
                "order_sn": order_sn,
                "package_number": package_number,
                "shipping_document_type": DOC_TYPE,
            }
        ]
    }
    create_resp = client._post("/api/v2/logistics/create_shipping_document", create_req)
    out["steps"]["create_shipping_document"] = {
        "request": create_req,
        "response": create_resp,
    }

    polls = []
    ready = False
    end = time.time() + POLL_TIMEOUT_SECONDS
    while time.time() < end:
        result_req = {
            "order_list": [
                {
                    "order_sn": order_sn,
                    "package_number": package_number,
                }
            ]
        }
        result_resp = client._post("/api/v2/logistics/get_shipping_document_result", result_req)
        first = ((result_resp.get("response") or {}).get("result_list") or [{}])[0]
        status = str(first.get("status") or "").upper()
        polls.append({
            "request": result_req,
            "response": result_resp,
            "status": status,
            "ts": int(time.time()),
        })
        if status == "READY":
            ready = True
            break
        time.sleep(POLL_SECONDS)

    out["steps"]["poll_result"] = {
        "ready": ready,
        "polls": polls,
    }

    download_req = {
        "order_list": [
            {
                "order_sn": order_sn,
                "package_number": package_number,
            }
        ]
    }
    if ready:
        download_resp = client._post("/api/v2/logistics/download_shipping_document", download_req)
        out["steps"]["download_shipping_document"] = {
            "request": download_req,
            "response": download_resp,
        }
    else:
        out["steps"]["download_shipping_document"] = {
            "request": download_req,
            "skipped": True,
            "reason": "not_ready_within_poll_timeout",
        }

    print(json.dumps(out, indent=2, ensure_ascii=True))


if __name__ == "__main__":
    main()
