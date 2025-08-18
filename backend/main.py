from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from scraper import run_once

app = FastAPI()

# CORS 設定，方便前端 fetch
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def home():
    """Render 會先打 / 來檢查服務有沒有啟動"""
    return {"message": "Service is running"}

@app.get("/api/status")
async def api_status(
    url: str = Query(None),
    urls: str = Query(None)
):
    """
    抓取票券狀態
    - url: 單一網址
    - urls: 多個網址，用逗號分隔
    """
    try:
        data = await run_once(url=url, urls=urls)
        return data
    except Exception as e:
        # 這樣即使爬蟲掛掉，也能回 JSON，不會變 500
        return {"results": [], "errors": [str(e)]}
