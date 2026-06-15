import json
from typing import Any, Dict, Tuple

from app.scrapers.shopee_api import ShopeeAPIClient

ORDER_SN = "2603289HF6CQUT"
PACKAGE_NUMBER = "OFG228328001282376"
SHIPPING_DOCUMENT_TYPE = "THERMAL_AIR_WAYBILL"
SHOP_ID = 411435241


def _pick_first_pickup(preflight: Dict[str, Any]) -> Tuple[int, str]:
    addresses = (((preflight.get("response") or {}).get("pickup") or {}).get("address_list") or [])
    for addr in addresses:
        address_id = int(addr.get("address_id") or 0)
        slots = addr.get("time_slot_list") or []
        for slot in slots:
            pickup_time_id = str(slot.get("pickup_time_id") or "").strip()
            if address_id > 0 and pickup_time_id:
                return address_id, pickup_time_id
    return 0, ""


def main():
    c = ShopeeAPIClient(SHOP_ID)
    out: Dict[str, Any] = {
        "order_sn": ORDER_SN,
        "package_number": PACKAGE_NUMBER,
        "shipping_document_type": SHIPPING_DOCUMENT_TYPE,
        "steps": {},
    }

    pre = c.get_shipping_parameter(ORDER_SN)
    addr_id, pickup_time_id = _pick_first_pickup(pre)
    out["steps"]["get_shipping_parameter"] = {
        "request": {
            "path": "/api/v2/logistics/get_shipping_parameter",
            "query": {"order_sn": ORDER_SN},
        },
        "response": pre,
        "extracted_pickup": {
            "address_id": addr_id,
            "pickup_time_id": pickup_time_id,
        },
    }

    ship_body = {
        "order_sn": ORDER_SN,
        "package_number": PACKAGE_NUMBER,
        "pickup": {
            "address_id": addr_id,
            "pickup_time_id": pickup_time_id,
        },
    }
    ship_resp = c._post("/api/v2/logistics/ship_order", ship_body)
    out["steps"]["ship_order"] = {
        "request": {
            "path": "/api/v2/logistics/ship_order",
            "body": ship_body,
        },
        "response": ship_resp,
    }

    create_body = {
        "order_list": [
            {
                "order_sn": ORDER_SN,
                "package_number": PACKAGE_NUMBER,
                "shipping_document_type": SHIPPING_DOCUMENT_TYPE,
            }
        ]
    }
    create_resp = c._post("/api/v2/logistics/create_shipping_document", create_body)
    out["steps"]["create_shipping_document"] = {
        "request": {
            "path": "/api/v2/logistics/create_shipping_document",
            "body": create_body,
        },
        "response": create_resp,
    }

    result_body = {
        "order_list": [
            {
                "order_sn": ORDER_SN,
                "package_number": PACKAGE_NUMBER,
            }
        ]
    }
    result_resp = c._post("/api/v2/logistics/get_shipping_document_result", result_body)
    out["steps"]["get_shipping_document_result_once"] = {
        "request": {
            "path": "/api/v2/logistics/get_shipping_document_result",
            "body": result_body,
        },
        "response": result_resp,
    }

    # Pull fail details from create payload for fast triage.
    first = (((create_resp.get("response") or {}).get("result_list") or [{}])[0])
    out["create_fail_detail"] = {
        "fail_error": first.get("fail_error"),
        "fail_message": first.get("fail_message"),
    }

    print(json.dumps(out, indent=2, ensure_ascii=True))


if __name__ == "__main__":
    main()
