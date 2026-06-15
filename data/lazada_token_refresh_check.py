import asyncio
from app.scrapers.lazada import LazadaScraper

async def main():
    s = LazadaScraper()
    await s.start()
    params = s._base_params('/auth/token/refresh')
    import httpx
    async with httpx.AsyncClient() as client:
        r = await client.post(s.AUTH_URL + s.REFRESH_PATH, params=params, timeout=30)
        print(r.text)

if __name__ == "__main__":
    asyncio.run(main())
