# scraper.py
import re, asyncio, os
from typing import List, Dict, Any
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 主要對外函式：支援單一 url 或逗號分隔的 urls
async def run_once(url: str | None = None, urls: str | None = None) -> Dict[str, Any]:
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
                    data = await _fetch_one(context, u)
                    results.append(data)
                except Exception as e:
                    errors.append({"url": u, "error": f"{type(e).__name__}: {e}"})
        finally:
            await context.close()
            await browser.close()
    return {"results": results, "errors": errors}

async def _fetch_one(context, url: str) -> Dict[str, Any]:
    page = await context.new_page()
    # 預設等待拉長，OpenTix 載入常超過 10~20s
    page.set_default_timeout(90_000)

    html = ""
    try:
        # 第一階段：先到 DOM 就緒
        await page.goto(url, wait_until="domcontentloaded", timeout=90_000)

        # 第二階段：嘗試等到網路空閒（如果等不到就放行）
        try:
            await page.wait_for_load_state("networkidle", timeout=30_000)
        except PWTimeout:
            pass

        # 可能有 cookie/提示彈窗，嘗試關掉（不影響失敗就算）
        for text in ("同意", "接受", "我知道了", "OK", "關閉"):
            try:
                await page.get_by_role("button", name=text).first.click(timeout=800)
                break
            except Exception:
                pass

        # 等待頁面上出現含「剩」的元素（若沒出現，不當成致命錯誤）
        try:
            await page.locator(":text('剩')").first.wait_for(timeout=30_000)
        except PWTimeout:
            pass

        # 抓取頁面 HTML
        html = await page.content()

        # 解析「剩：xxx」；同時抓常見中文全形數字/逗號
        entries = []
        for m in re.finditer(r"(?:剩|餘)[:：]\\s*([0-9０-９,，]+)", html):
            remaining = m.group(1).translate(str.maketrans("０１２３４５６７８９，", "0123456789,"))  # 轉半形
            entries.append({"label": None, "remaining": remaining})

        # 解析不到時，直接回報「未偵測到」
        if not entries:
            return {"url": url, "entries": []}
        return {"url": url, "entries": entries}

    except PWTimeout as e:
        # 專門回報 timeout，前端會顯示在下方紅字
        raise PWTimeout(f"Timeout while loading {url}: {e}") from e
    finally:
        await page.close()
