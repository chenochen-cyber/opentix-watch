# scraper.py
# -*- coding: utf-8 -*-

import asyncio
from typing import List, Dict, Any, Optional, Tuple
from contextlib import asynccontextmanager

from playwright.async_api import async_playwright, TimeoutError as PWTimeout


DEFAULT_TIMEOUT_MS = 45_000  # goto / wait timeout
WAIT_FOR_REMAIN_MS = 30_000  # 額外等「剩」字樣的時間


@asynccontextmanager
async def _browser_context(headless: bool = True):
    """
    建立並管理 Playwright Browser 與 Context。
    """
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 960},
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"),
        )
        try:
            yield context
        finally:
            await context.close()
            await browser.close()


async def _parse_event_blocks(page) -> List[Dict[str, Any]]:
    """
    從已載入的頁面解析多個場次區塊，擷取「場次標籤」與「剩餘」。
    回傳 entries: [{"label": "...", "remaining": "剩：xxx"}, ...]
    """
    entries: List[Dict[str, Any]] = []

    # 主要容器：.events__list__table .column__body
    blocks = await page.query_selector_all(".events__list__table .column__body")
    if not blocks:
        # 備援：有些頁面 class 名稱不同
        blocks = await page.query_selector_all(".events__list .column__body, .events__list__table [class*=column__body]")

    for block in blocks:
        # 場次主標籤（通常是日期＋時間）
        label = None
        # 1) 優先抓日期列的第一段文字
        date_span = await block.query_selector(".date span, .date")
        if date_span:
            txt = (await date_span.inner_text()).strip()
            # 只取第一行（避免帶出場館、icon等）
            label = txt.splitlines()[0].strip()

        # 2) 如果還是沒有，就退而求其次抓整個 left 區
        if not label:
            left = await block.query_selector(".left")
            if left:
                txt = (await left.inner_text()).strip()
                # 仍只取第一行當作標籤
                label = txt.splitlines()[0].strip()

        if not label:
            label = "場次"

        # 剩餘票數
        remaining_text = "—"
        remain_span = await block.query_selector(".remain_infos span, .remain_infos")
        if remain_span:
            t = (await remain_span.inner_text()).strip()
            # 正常應該長這樣：剩：910
            if "剩" in t:
                remaining_text = t
            else:
                # 保底：直接回傳原字串
                remaining_text = t

        entries.append({
            "label": label,
            "remaining": remaining_text
        })

    return entries


async def scrape_event_page(url: str,
                            timeout_ms: int = DEFAULT_TIMEOUT_MS,
                            headless: bool = True) -> Dict[str, Any]:
    """
    抓取單一 OpenTix 活動頁面的「剩餘票數」資訊。
    回傳格式：
    {
      "url": url,
      "entries": [{"label":"2025/12/13 (六) 19:30", "remaining":"剩：910"}, ...],
      "error": "..."  # 若有錯誤
    }
    """
    result: Dict[str, Any] = {"url": url, "entries": []}

    try:
        async with _browser_context(headless=headless) as context:
            page = await context.new_page()

            # 進站與基本等待
            await page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            # 等到網路空閒（避免 skeleton 未完）
            try:
                await page.wait_for_load_state("networkidle", timeout=timeout_ms)
            except PWTimeout:
                # 有時部分站點資源長連線，忽略
                pass

            # 額外等待「剩」字樣；若抓不到也不致失敗（容錯）
            try:
                await page.get_by_text("剩", exact=False).first.wait_for(timeout=WAIT_FOR_REMAIN_MS)
            except PWTimeout:
                # 容忍無「剩」字樣，改走直接解析
                pass

            # 解析
            entries = await _parse_event_blocks(page)

            # 如果 entries 皆為「—」，有時是因為價格區尚未展開或 DOM 還在換頁，嘗試再等一下重撈一次
            if not entries or all(e.get("remaining", "—") == "—" for e in entries):
                await page.wait_for_timeout(1000)  # 1s 小延遲
                entries = await _parse_event_blocks(page)

            result["entries"] = entries
            return result

    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        return result


async def scrape_event_pages(urls: List[str],
                             timeout_ms: int = DEFAULT_TIMEOUT_MS,
                             headless: bool = True) -> Dict[str, Any]:
    """
    並行抓取多個活動頁面。
    回傳格式：
    {
      "results": [ {單頁結果}, {單頁結果}, ... ],
      "errors": ["...", "..."]  # 若有錯誤
    }
    """
    results: List[Dict[str, Any]] = []
    errors: List[str] = []

    # 並行執行
    tasks = [
        scrape_event_page(u.strip(), timeout_ms=timeout_ms, headless=headless)
        for u in urls if u and u.strip()
    ]
    if not tasks:
        return {"results": [], "errors": ["沒有可抓取的 URL"]}

    pages = await asyncio.gather(*tasks, return_exceptions=True)
    for p in pages:
        if isinstance(p, Exception):
            errors.append(f"{type(p).__name__}: {p}")
        else:
            results.append(p)
            if p.get("error"):
                errors.append(f"{p['url']} {p['error']}")

    return {"results": results, "errors": errors}


# ---- 提供給 FastAPI / 其他同步環境呼叫的包裝 ----

def scrape_status(urls: List[str],
                  timeout_ms: int = DEFAULT_TIMEOUT_MS,
                  headless: bool = True) -> Dict[str, Any]:
    """
    同步包裝，便於 FastAPI 同步路由呼叫（內部起 event loop）。
    """
    return asyncio.run(scrape_event_pages(urls, timeout_ms=timeout_ms, headless=headless))
