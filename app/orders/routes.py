from fastapi import APIRouter

from app.api.routes import (
    lazada_arrange_shipment_order,
    lazada_create_awb_order,
    lazada_get_awb_order,
    shopee_get_awb_order,
    shopee_get_awb_parameter_order,
    shopee_get_tracking_order,
    tiktok_arrange_shipment_order,
    tiktok_create_awb_order,
    tiktok_get_awb_order,
)

router = APIRouter(tags=["orders"])

router.add_api_route("/orders/shopee/{order_id}/awb", shopee_get_awb_order, methods=["GET"])
router.add_api_route("/orders/shopee/{order_id}/awb/parameter", shopee_get_awb_parameter_order, methods=["GET"])
router.add_api_route("/orders/shopee/{order_id}/tracking", shopee_get_tracking_order, methods=["GET"])
router.add_api_route("/orders/lazada/{order_id}/arrange-shipment", lazada_arrange_shipment_order, methods=["POST"])
router.add_api_route("/orders/lazada/{order_id}/awb/create", lazada_create_awb_order, methods=["POST"])
router.add_api_route("/orders/lazada/{order_id}/awb", lazada_get_awb_order, methods=["GET"])
router.add_api_route("/orders/tiktok/{order_id}/arrange-shipment", tiktok_arrange_shipment_order, methods=["POST"])
router.add_api_route("/orders/tiktok/{order_id}/awb/create", tiktok_create_awb_order, methods=["POST"])
router.add_api_route("/orders/tiktok/{order_id}/awb", tiktok_get_awb_order, methods=["GET"])
