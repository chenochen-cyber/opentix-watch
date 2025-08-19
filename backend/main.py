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

# === Online users (heartbeat) ===
from pydantic import BaseModel
import time
from typing import Dict

ONLINE_SEEN: Dict[str, float] = {}
ONLINE_TTL_SECONDS = 60  # 幾秒內算在線

class Heartbeat(BaseModel):
    client_id: str

@app.post("/heartbeat")
async def heartbeat(hb: Heartbeat):
    """前端每 20 秒呼叫一次，回報使用者仍在線。"""
    ONLINE_SEEN[hb.client_id] = time.time()
    return {"ok": True}

@app.get("/online_count")
async def online_count():
    """回傳最近 ONLINE_TTL_SECONDS 秒內有回報的使用者數量。"""
    now = time.time()
    # 清掉過期
    for cid, ts in list(ONLINE_SEEN.items()):
        if now - ts > ONLINE_TTL_SECONDS:
            ONLINE_SEEN.pop(cid, None)
    # 計算在線
    return {"count": sum(1 for ts in ONLINE_SEEN.values() if now - ts <= ONLINE_TTL_SECONDS)}

