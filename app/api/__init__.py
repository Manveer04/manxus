from fastapi import APIRouter

from app.inventory.routes import router as inventory_router
from app.orders.routes import router as orders_router

router = APIRouter()
router.include_router(inventory_router, prefix="/api")
router.include_router(orders_router, prefix="/api")
from app.api.routes import router
