from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from typing import Optional
from scraper import run_once, scrape_status, scrape_event_pages

app = FastAPI()

@app.get("/api/status")
async def api_status(
    url: Optional[str] = Query(None),
    urls: Optional[str] = Query(None)
):
    """
    抓取票券剩餘數資訊
    - url: 單一網址
    - urls: 多個網址，用逗號分隔
    """
    try:
        data = await run_once(url=url, urls=urls)
        return JSONResponse(content=data)
    except Exception as e:
        # 捕捉所有錯誤，避免 500，回傳清楚的錯誤訊息
        return JSONResponse(
            content={"results": [], "errors": [str(e)]},
            status_code=200
        )

@app.get("/health")
async def health():
    """健康檢查用"""
    return {"status": "ok"}
