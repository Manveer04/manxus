import asyncio
import httpx
import os
import json

ORDER_ITEM_IDS = ["499300446564142"]
SHIPPING_PROVIDERS = ["J&T Express"]

async def main():
    BASE_URL = "https://api.lazada.com.my/rest"
    PATH = "/order/pack"
    app_key = os.environ["LAZADA_APP_KEY"]
    app_secret = os.environ["LAZADA_APP_SECRET"]
    # Load access_token from session file
    session_path = os.path.join(os.path.dirname(__file__), "..", "sessions", "lazada.json")
    with open(session_path, "r") as f:
        session = json.load(f)
        access_token = session["access_token"]
    import time
    def sign(path, params):
        sorted_kv = "".join(f"{k}{v}" for k, v in sorted(params.items()) if k != "sign")
        import hmac, hashlib
        return hmac.new(app_secret.encode("utf-8"), (path + sorted_kv).encode("utf-8"), hashlib.sha256).hexdigest().upper()
    params = {
        "app_key": app_key,
        "timestamp": str(int(time.time() * 1000)),
        "sign_method": "sha256",
        "access_token": access_token,
        "orderItemIds": json.dumps(ORDER_ITEM_IDS),
        "shippingProviders": json.dumps(SHIPPING_PROVIDERS),
    }
    params["sign"] = sign(PATH, params)
    async with httpx.AsyncClient() as client:
        r = await client.post(BASE_URL + PATH, data=params, timeout=30)
        print("REQUEST:")
        print(json.dumps(params, indent=2))
        print("RESPONSE:")
        print(r.text)

if __name__ == "__main__":
    asyncio.run(main())
