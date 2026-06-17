from typing import List

from fastapi import APIRouter

from app.api.routes import (
    CreateGroupRequest,
    GroupOut,
    SyncLogOut,
    create_group,
    delete_group,
    get_document_image,
    get_group,
    get_group_image,
    get_logs,
    get_product,
    get_product_image,
    get_stats,
    list_groups,
    list_products,
    platform_status,
    push_group_stock,
    push_out_of_sync,
    push_stock,
    save_tracker_urls,
    sync_all_inventory_route,
    sync_platform_inventory_route,
    suggest_groups,
    tracker_history,
    tracker_products,
    tracker_sync,
    tracker_urls,
    update_group,
    update_product,
    upload_group_image,
    upload_product_image,
    ProductOut,
    UpdateGroupRequest,
    UpdateProductRequest,
    UpdateStockRequest,
)

router = APIRouter(tags=["inventory"])

router.add_api_route("/products", list_products, methods=["GET"], response_model=List[ProductOut])
router.add_api_route("/products/{product_id}", get_product, methods=["GET"], response_model=ProductOut)
router.add_api_route("/products/{product_id}", update_product, methods=["PATCH"], response_model=ProductOut)
router.add_api_route("/groups", list_groups, methods=["GET"], response_model=List[GroupOut])
router.add_api_route("/groups", create_group, methods=["POST"], response_model=GroupOut)
router.add_api_route("/groups/suggest", suggest_groups, methods=["GET"])
router.add_api_route("/groups/{group_id}", get_group, methods=["GET"], response_model=GroupOut)
router.add_api_route("/groups/{group_id}", update_group, methods=["PATCH"], response_model=GroupOut)
router.add_api_route("/groups/{group_id}", delete_group, methods=["DELETE"])
router.add_api_route("/groups/{group_id}/image", upload_group_image, methods=["POST"])
router.add_api_route("/groups/{group_id}/image/file", get_group_image, methods=["GET"])
router.add_api_route("/groups/{group_id}/push", push_group_stock, methods=["POST"])
router.add_api_route("/products/{product_id}/push", push_stock, methods=["POST"])
router.add_api_route("/sync/pull-all", sync_all_inventory_route, methods=["POST"])
router.add_api_route("/sync/pull/{platform}", sync_platform_inventory_route, methods=["POST"])
router.add_api_route("/sync/push-out-of-sync", push_out_of_sync, methods=["POST"])
router.add_api_route("/tracker/products", tracker_products, methods=["GET"])
router.add_api_route("/tracker/history", tracker_history, methods=["GET"])
router.add_api_route("/tracker/urls", tracker_urls, methods=["GET"])
router.add_api_route("/tracker/urls", save_tracker_urls, methods=["POST"], response_model=None)
router.add_api_route("/tracker/sync", tracker_sync, methods=["POST"])
router.add_api_route("/platforms/status", platform_status, methods=["GET"])
router.add_api_route("/logs", get_logs, methods=["GET"], response_model=List[SyncLogOut])
router.add_api_route("/stats", get_stats, methods=["GET"])
router.add_api_route("/products/{product_id}/image", upload_product_image, methods=["POST"])
router.add_api_route("/products/{product_id}/image/file", get_product_image, methods=["GET"])
router.add_api_route("/documents/image/{filename}", get_document_image, methods=["GET"])
