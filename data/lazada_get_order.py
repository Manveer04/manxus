import asyncio
import httpx
import os

ORDER_ID = "499300446464142"

async def main():
    BASE_URL = "https://api.lazada.com.my/rest"
    PATH = "/order/get"
    app_key = os.environ["LAZADA_APP_KEY"]
    app_secret = os.environ["LAZADA_APP_SECRET"]
    access_token = None
    # Load access_token from session file
    import json
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
        "order_id": ORDER_ID,
    }
    params["sign"] = sign(PATH, params)
    async with httpx.AsyncClient() as client:
        r = await client.get(BASE_URL + PATH, params=params, timeout=30)
        print(r.text)

if __name__ == "__main__":
    asyncio.run(main())
