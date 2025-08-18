# backend/scraper.py
import re
import asyncio
import os
from typing import List, Dict, Any, Optional
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# 確保瀏覽器安裝路徑（避免 Render 的預設快取被清掉）
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/render/project/.playwright")

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# -------- 對外：單次執行（支援 ?url= 或 ?urls= 逗號分隔） --------
async def run_once(url: Optional[str] = None, urls: Optional[str] = None) -> Dict[str, Any]:
    targets: List[str] = []
    if urls:
        targets = [u.strip() for u in urls.split(",") if u.strip()]
    elif url:
        targets = [url.strip()]
    else:
        return {"results": [], "errors": ["no url provided"]}

    results, errors = [], []
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        context = await browser.new_context(
            user_agent=UA,
            locale="zh-TW",
            viewport={"width": 1280, "height": 900},
            ignore_https_errors=True,
            extra_http_headers={
                "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
                "Upgrade-Insecure-Requests": "1",
            },
        )
        try:
            for u in targets:
                try:
                    data = await _try_fetch(context, u, tries=2)  # ← 逾時會自動重試一次
                    results.append(data)
                except Exception as e:
                    errors.append({"url": u, "error": f"{type(e).__name__}: {e}"})
        finally:
            await context.close()
            await browser.close()
    return {"results": results, "errors": errors}

# -------- 單一 URL：帶重試包裝 --------
async def _try_fetch(context, url: str, tries: int = 2) -> Dict[str, Any]:
    last_exc: Optional[Exception] = None
    for i in range(tries):
        try:
            return await _fetch_one(context, url)
        except Exception as e:
            last_exc = e
            # 小退避（第2次前等一下）
            if i + 1 < tries:
                await asyncio.sleep(2.0)
    assert last_exc is not None
    raise last_exc

# -------- 真正的抓取與解析 --------
async def _fetch_one(context, url: str) -> Dict[str, Any]:
    page = await context.new_page()
    # OpenTix 載入常偏慢：把預設等待拉長
    page.set_default_timeout(90_000)

    try:
        # 階段 1：先等 DOM ready
        await page.goto(url, wait_until="domcontentloaded", timeout=90_000)

        # 階段 2：再嘗試等到 networkidle（等不到就跳過）
        try:
            await page.wait_for_load_state("networkidle", timeout=45_000)
        except PWTimeout:
            pass

        # 可能彈 cookie 同意 / 提示，嘗試關掉，不影響失敗也無妨
        for text in ("同意", "接受", "我知道了", "OK", "關閉"):
            try:
                await page.get_by_role("button", name=text).first.click(timeout=800)
                break
            except Exception:
                pass

        # 再等頁面上出現包含「剩」字樣的元素（若沒出現不當致命）
        try:
            await page.locator(":text('剩')").first.wait_for(timeout=30_000)
        except PWTimeout:
            pass

        html = await page.content()

        # 解析「剩：xxx」；支援全形數字與逗號
        entries = []
        for m in re.finditer(r"(?:剩|餘)[:：]\s*([0-9０-９,，]+)", html):
            remaining = m.group(1).translate(
                str.maketrans("０１２３４５６７８９，", "0123456789,")
            )
            entries.append({"label": None, "remaining": remaining})

        # 若沒抓到任何「剩」，回傳空 entries（前端會顯示「未偵測到」）
        if not entries:
            return {"url": url, "entries": []}

        return {"url": url, "entries": entries}

    except PWTimeout as e:
        # 逾時錯誤，讓外層包起來回傳到 errors
        raise PWTimeout(f"Timeout while loading {url}: {e}") from e
    finally:
        await page.close()
