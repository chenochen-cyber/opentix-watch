# backend/scraper.py
import os
import re
import asyncio
from typing import List, Dict, Any, Optional
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# Render/雲端常見：固定瀏覽器安裝路徑（避免跑到 ephemeral cache）
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/render/project/.playwright")

# 一般桌機 UA（中文語系），有些站會依 UA/語系載不同區塊
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# ===== 對外主函式：支援 ?url= 或 ?urls=（逗號分隔） =====
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
        # 雲端容器通常要 --no-sandbox / --disable-dev-shm-usage
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
                    data = await _try_fetch(context, u, tries=2)  # 逾時/偶發錯誤重試 1 次
                    results.append(data)
                except Exception as e:
                    errors.append({"url": u, "error": f"{type(e).__name__}: {e}"})
        finally:
            await context.close()
            await browser.close()

    return {"results": results, "errors": errors}

# ===== 重試包裝 =====
async def _try_fetch(context, url: str, tries: int = 2) -> Dict[str, Any]:
    last_exc: Optional[Exception] = None
    for i in range(tries):
        try:
            return await _fetch_one(context, url)
        except Exception as e:
            last_exc = e
            if i + 1 < tries:
                await asyncio.sleep(2.0)  # 小退避
    assert last_exc is not None
    raise last_exc

# ===== 單頁抓取與解析（依你提供的 DOM 結構） =====
async def _fetch_one(context, url: str) -> Dict[str, Any]:
    page = await context.new_page()
    page.set_default_timeout(90_000)  # OpenTix 載入常偏慢

    try:
        # 階段 1：進頁面（先到 DOM ready）
        await page.goto(url, wait_until="domcontentloaded", timeout=90_000)

        # 階段 2：嘗試等到主要清單區塊出現（SPA 完整渲染）
        try:
            await page.locator(".events__list__table").first.wait_for(timeout=45_000)
        except PWTimeout:
            # 兜底：再等 networkidle 一下
            try:
                await page.wait_for_load_state("networkidle", timeout=15_000)
            except PWTimeout:
                pass

        # 可能有 cookie/提示彈窗，嘗試關掉（失敗就算）
        for text in ("同意", "接受", "我知道了", "OK", "關閉"):
            try:
                await page.get_by_role("button", name=text).first.click(timeout=800)
                break
            except Exception:
                pass

        # 逐行解析：每個場次都在 .column__body
        rows = page.locator(".events__list__table .column__body")
        count = await rows.count()
        entries = []

        def normalize_digits(s: str) -> str:
            # 全形->半形；保留逗號
            return s.translate(str.maketrans("０１２３４５６７８９，", "0123456789,"))

        for i in range(count):
            row = rows.nth(i)

            # 場次日期（例：2025/8/29 (五) 19:30）
            date_txt = ""
            try:
                date_txt = (await row.locator(".date .mr-2").first.inner_text()).strip()
            except Exception:
                pass

            # 說明（例：彩蛋場，可能不存在）
            desc_txt = ""
            try:
                desc_txt = (await row.locator(".date .description").first.inner_text()).strip()
            except Exception:
                pass

            # 剩餘（例：剩：91）位於 .priceplans_wrapper .remain_infos > span
            remain_txt = ""
            try:
                remain_txt = (await row.locator(".priceplans_wrapper .remain_infos > span").first.inner_text()).strip()
            except Exception:
                pass

            # 從「剩：91」抽數字（支援全形）
            m = re.search(r"([0-9０-９,，]+)", remain_txt)
            if m:
                remaining = normalize_digits(m.group(1))
                label = f"{date_txt} {desc_txt}".strip()
                entries.append({"label": label or None, "remaining": remaining})

        # 即使沒有偵測到，也回傳 entries=[]，讓前端顯示「未偵測到」
        return {"url": url, "entries": entries}

    except PWTimeout as e:
        # 逾時錯誤明確回傳（外層會彙整到 errors）
        raise PWTimeout(f"Timeout while loading {url}: {e}") from e
    finally:
        await page.close()
