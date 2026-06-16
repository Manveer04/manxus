import json
import time
from typing import Any, Dict, List

from app.scrapers.shopee_api import ShopeeAPIClient

ORDER_SN = "2603289HF6CQUT"
SHOP_ID = 411435241
EXPLICIT_DOC_TYPE = "THERMAL_AIR_WAYBILL"


def _first_order_logistics(payload: Dict[str, Any]) -> Dict[str, Any]:
    response = payload.get("response") or {}
    logistics = response.get("logistics") or []
    if logistics:
        return logistics[0]
    return response


def _extract_package_number(order_logistics_payload: Dict[str, Any], shipping_param_payload: Dict[str, Any]) -> str:
    ol = _first_order_logistics(order_logistics_payload)
    for key in ("package_number", "package_id", "parcel_number"):
        v = ol.get(key)
        if v is not None and str(v).strip():
            return str(v).strip()

    sp_response = shipping_param_payload.get("response") or {}
    for key in ("package_number", "parcel_number"):
        v = sp_response.get(key)
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def _extract_supported_doc_types(shipping_doc_param_payload: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    response = shipping_doc_param_payload.get("response") or {}
    result_list = response.get("result_list") or []
    if result_list:
        first = result_list[0] or {}
        suggested = first.get("suggest_shipping_document_type")
        selected = first.get("selected_shipping_document_type")
        for src in (suggested, selected):
            if isinstance(src, list):
                for item in src:
                    if item is not None and str(item).strip():
                        out.append(str(item).strip())
            elif src is not None and str(src).strip():
                out.append(str(src).strip())

    dedup: List[str] = []
    seen = set()
    for t in out:
        if t in seen:
            continue
        seen.add(t)
        dedup.append(t)
    return dedup


def _extract_channel(order_logistics_payload: Dict[str, Any]) -> Dict[str, Any]:
    ol = _first_order_logistics(order_logistics_payload)
    return {
        "logistics_channel_id": ol.get("logistics_channel_id") or ol.get("shipping_carrier") or ol.get("channel_id"),
        "logistics_channel_name": ol.get("logistics_channel") or ol.get("shipping_carrier_name") or ol.get("channel_name"),
        "raw": ol,
    }


def main():
    client = ShopeeAPIClient(SHOP_ID)

    out: Dict[str, Any] = {
        "order_sn": ORDER_SN,
        "shop_id": SHOP_ID,
        "explicit_shipping_document_type": EXPLICIT_DOC_TYPE,
        "calls": {},
        "extracted": {},
        "retry_create": {},
        "capability_assessment": {},
    }

    shipping_param = client.get_shipping_parameter(ORDER_SN)
    out["calls"]["get_shipping_parameter"] = {
        "sent": {"order_sn": ORDER_SN},
        "response": shipping_param,
    }

    order_logistics = client.get_order_logistics(ORDER_SN)
    out["calls"]["get_order_logistics"] = {
        "sent": {"order_sn": ORDER_SN},
        "response": order_logistics,
    }

    package_number = _extract_package_number(order_logistics, shipping_param)
    channel = _extract_channel(order_logistics)

    shipping_doc_param = client.get_shipping_document_parameter(ORDER_SN, package_number)
    out["calls"]["get_shipping_document_parameter"] = {
        "sent": {"order_sn": ORDER_SN, "package_number": package_number},
        "response": shipping_doc_param,
    }

    supported_doc_types = _extract_supported_doc_types(shipping_doc_param)

    out["extracted"] = {
        "package_number": package_number,
        "channel": channel,
        "supported_shipping_document_types": supported_doc_types,
    }

    create_resp = client.create_shipping_document(
        ORDER_SN,
        package_number=package_number,
        shipping_document_type=EXPLICIT_DOC_TYPE,
    )
    out["retry_create"]["create_shipping_document"] = {
        "sent": {
            "order_sn": ORDER_SN,
            "package_number": package_number,
            "shipping_document_type": EXPLICIT_DOC_TYPE,
        },
        "response": create_resp,
    }

    # Poll result briefly to see if document gets ready.
    poll_records = []
    for _ in range(5):
        result = client.get_shipping_document_result(ORDER_SN, package_number)
        poll_records.append(result)
        time.sleep(2)
    out["retry_create"]["get_shipping_document_result_polls"] = poll_records

    # Try download explicitly after polling.
    download_resp = client.download_shipping_document(ORDER_SN, package_number)
    out["retry_create"]["download_shipping_document"] = {
        "sent": {"order_sn": ORDER_SN, "package_number": package_number},
        "response": download_resp,
    }

    create_first_error = ""
    try:
        create_first_error = str((((create_resp.get("response") or {}).get("result_list") or [{}])[0].get("fail_error")) or "")
    except Exception:
        create_first_error = ""

    out["capability_assessment"] = {
        "channel_name": channel.get("logistics_channel_name"),
        "channel_id": channel.get("logistics_channel_id"),
        "explicit_doc_type_requested": EXPLICIT_DOC_TYPE,
        "doc_type_list_from_parameter": supported_doc_types,
        "explicit_doc_type_listed": EXPLICIT_DOC_TYPE in supported_doc_types if supported_doc_types else False,
        "create_fail_error": create_first_error,
        "api_printing_supported_inferred": not (create_first_error in ("logistics.package_can_not_print", "logistics.channel_not_support") and not supported_doc_types),
    }

    print(json.dumps(out, indent=2, ensure_ascii=True))


if __name__ == "__main__":
    main()
