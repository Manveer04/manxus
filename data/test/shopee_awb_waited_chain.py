import json
import time

from app.scrapers.shopee_api import ShopeeAPIClient

ORDER_SN = "2603289HF6CQUT"
PACKAGE_NUMBER = "OFG228328001282376"
DOC_TYPE = "THERMAL_AIR_WAYBILL"
SHOP_ID = 411435241


def main():
    c = ShopeeAPIClient(SHOP_ID)
    out = {
        "order_sn": ORDER_SN,
        "package_number": PACKAGE_NUMBER,
        "shipping_document_type": DOC_TYPE,
        "steps": {},
    }

    create_body = {
        "order_list": [
            {
                "order_sn": ORDER_SN,
                "package_number": PACKAGE_NUMBER,
                "shipping_document_type": DOC_TYPE,
            }
        ]
    }
    create_resp = c._post("/api/v2/logistics/create_shipping_document", create_body)
    out["steps"]["create_shipping_document"] = {
        "request": create_body,
        "response": create_resp,
    }

    polls = []
    ready = False
    end = time.time() + 300
    while time.time() < end:
        result_body = {
            "order_list": [
                {
                    "order_sn": ORDER_SN,
                    "package_number": PACKAGE_NUMBER,
                }
            ]
        }
        result_resp = c._post("/api/v2/logistics/get_shipping_document_result", result_body)
        first = ((result_resp.get("response") or {}).get("result_list") or [{}])[0]
        status = str(first.get("status") or "").upper()
        polls.append(
            {
                "request": result_body,
                "response": result_resp,
                "status": status,
                "ts": int(time.time()),
            }
        )
        if status == "READY":
            ready = True
            break
        time.sleep(5)

    out["steps"]["get_shipping_document_result_polls"] = polls

    download_body = {
        "order_list": [
            {
                "order_sn": ORDER_SN,
                "package_number": PACKAGE_NUMBER,
            }
        ]
    }
    if ready:
        download_resp = c._post("/api/v2/logistics/download_shipping_document", download_body)
        out["steps"]["download_shipping_document"] = {
            "request": download_body,
            "response": download_resp,
        }
    else:
        out["steps"]["download_shipping_document"] = {
            "request": download_body,
            "skipped": True,
            "reason": "status_not_ready_within_poll_window",
        }

    print(json.dumps(out, indent=2, ensure_ascii=True))


if __name__ == "__main__":
    main()
