import asyncio
import json

from app.scrapers.lazada import LazadaScraper

ORDER_ID = "499300446464142"
ORDER_ITEM_ID = "499300446564142"


async def main():
    s = LazadaScraper()
    await s.start()
    try:
        out = {}
        out["order_get"] = await s._request("/order/get", "GET", {"order_id": ORDER_ID})
        out["set_status_to_packed_csv"] = await s._request(
            "/order/SetStatusToPackedByMarketplace",
            "POST",
            {"order_item_ids": ORDER_ITEM_ID},
        )

        probe_paths = [
            "/seller/permissions/get",
            "/permission/item/get",
            "/auth/permission/get",
            "/app/scopes/get",
            "/seller/get",
        ]
        probes = []
        for p in probe_paths:
            try:
                probes.append({"path": p, "response": await s._request(p, "GET", {})})
            except Exception as e:
                probes.append({"path": p, "error": str(e)})
        out["permission_probes"] = probes
        print(json.dumps(out, indent=2, ensure_ascii=True))
    finally:
        await s.close()


if __name__ == "__main__":
    asyncio.run(main())
