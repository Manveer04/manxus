import json
import time
from typing import Any, Dict

from app.scrapers.shopee_api import ShopeeAPIClient

ORDER_SN = "2603289HF6CQUT"
PACKAGE_NUMBER = "OFG228328001282376"
SHIPPING_DOCUMENT_TYPE = "THERMAL_AIR_WAYBILL"
LOGISTICS_CHANNEL_ID = 20044
WAIT_SECONDS = 60
POLL_SECONDS = 5
SHOP_ID = 411435241


def _extract_marker(result_payload: Dict[str, Any]) -> str:
    # Prefer explicit status inside result_list when present.
    try:
        first = ((result_payload.get("response") or {}).get("result_list") or [{}])[0]
        status = str(first.get("status") or "").strip()
        if status:
            return f"status:{status}"
    except Exception:
        pass

    err = str(result_payload.get("error") or "").strip()
    msg = str(result_payload.get("message") or "").strip()
    if err or msg:
        return f"error:{err}|message:{msg}"
    return "unknown"


def main():
    client = ShopeeAPIClient(SHOP_ID)

    out: Dict[str, Any] = {
        "order_sn": ORDER_SN,
        "shop_id": SHOP_ID,
        "inputs": {
            "package_number": PACKAGE_NUMBER,
            "shipping_document_type": SHIPPING_DOCUMENT_TYPE,
            "logistics_channel_id": LOGISTICS_CHANNEL_ID,
            "wait_seconds": WAIT_SECONDS,
            "poll_seconds": POLL_SECONDS,
        },
        "steps": {
            "create_shipping_document": {},
            "get_shipping_document_result_polls": [],
            "download_shipping_document": {},
        },
        "create_fail_detail": {},
    }

    create_body = {
        "order_list": [
            {
                "order_sn": ORDER_SN,
                "package_number": PACKAGE_NUMBER,
                "shipping_document_type": SHIPPING_DOCUMENT_TYPE,
                "logistics_channel_id": LOGISTICS_CHANNEL_ID,
            }
        ]
    }
    create_resp = client._post("/api/v2/logistics/create_shipping_document", create_body)

    out["steps"]["create_shipping_document"] = {
        "request": {
            "path": "/api/v2/logistics/create_shipping_document",
            "body": create_body,
        },
        "response": create_resp,
    }

    # Extract Shopee fail details when batch API failed.
    try:
        first = ((create_resp.get("response") or {}).get("result_list") or [{}])[0]
        out["create_fail_detail"] = {
            "fail_error": first.get("fail_error"),
            "fail_message": first.get("fail_message"),
        }
    except Exception:
        out["create_fail_detail"] = {}

    deadline = time.time() + WAIT_SECONDS
    first_marker = None

    while True:
        poll_body = {
            "order_list": [
                {
                    "order_sn": ORDER_SN,
                    "package_number": PACKAGE_NUMBER,
                }
            ]
        }
        poll_resp = client._post("/api/v2/logistics/get_shipping_document_result", poll_body)
        marker = _extract_marker(poll_resp)

        out["steps"]["get_shipping_document_result_polls"].append(
            {
                "request": {
                    "path": "/api/v2/logistics/get_shipping_document_result",
                    "body": poll_body,
                },
                "response": poll_resp,
                "marker": marker,
                "timestamp": int(time.time()),
            }
        )

        if first_marker is None:
            first_marker = marker
        elif marker != first_marker:
            break

        if time.time() >= deadline:
            break

        time.sleep(POLL_SECONDS)

    download_body = {
        "order_list": [
            {
                "order_sn": ORDER_SN,
                "package_number": PACKAGE_NUMBER,
            }
        ]
    }
    download_resp = client._post("/api/v2/logistics/download_shipping_document", download_body)

    out["steps"]["download_shipping_document"] = {
        "request": {
            "path": "/api/v2/logistics/download_shipping_document",
            "body": download_body,
        },
        "response": download_resp,
    }

    print(json.dumps(out, indent=2, ensure_ascii=True))


if __name__ == "__main__":
    main()
