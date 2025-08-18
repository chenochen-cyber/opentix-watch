# main.py
# -*- coding: utf-8 -*-

from typing import List, Optional
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# 為了相容舊版 import 錯誤訊息，這裡同時導出兩個名稱
from scraper import scrape_status as _scrape_status, scrape_event_pages as _scrape_event_pages  # type: ignore

app = FastAPI(title="OpenTix Watch API", version="1.0.0")

# CORS：允許本機前端連線
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 本機測試方便；若上線可改精準網域
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class StatusResponse(BaseModel):
    results: list
    errors: list


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/api/status", response_model=StatusResponse)
def api_status(
    url: Optional[str] = Query(None, description="單一 OpenTix 活動網址"),
    urls: Optional[str] = Query(None, description="多個網址，以逗號分隔"),
):
    """
    與前端 index.html 對應的 API：
      - /api/status?url=...
      - /api/status?urls=url1,url2,...
    回傳：
      { results: [...], errors: [...] }
    """
    targets: List[str] = []
    if urls:
        targets = [u.strip() for u in urls.split(",") if u.strip()]
    elif url:
        targets = [url.strip()]

    if not targets:
        return {"results": [], "errors": ["請提供 url 或 urls 參數"]}

    # 預設 headless=True；若要除錯可改 False
    data = _scrape_status(targets, headless=True)
    return data


# ---- 相容舊程式碼的名稱（避免 ImportError）----

# 有些舊版 main 可能從 scraper 匯入 scrape_event_pages 後自己跑 loop。
# 這裡保留一個端點可直接間接呼叫，若你不需要可忽略。
@app.get("/api/debug-scrape")
async def api_debug_scrape(
    url: Optional[str] = Query(None),
    urls: Optional[str] = Query(None),
):
    targets: List[str] = []
    if urls:
        targets = [u.strip() for u in urls.split(",") if u.strip()]
    elif url:
        targets = [url.strip()]
    if not targets:
        return {"results": [], "errors": ["請提供 url 或 urls 參數"]}

    # 直接呼叫原生 async 版本（方便你在瀏覽器直接測試）
    data = await _scrape_event_pages(targets, headless=True)
    return data
