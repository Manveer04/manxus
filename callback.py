from fastapi import FastAPI, Request

app = FastAPI()

@app.get("/tiktok/callback")
async def callback(request: Request):
    params = dict(request.query_params)
    return {"received": params}
