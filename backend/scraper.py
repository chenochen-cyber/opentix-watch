# backend/scraper.py
import os
import re
import asyncio
import logging
import time
from typing import List, Dict, Any, Optional, Union
from dataclasses import dataclass
from urllib.parse import urlparse

from playwright.async_api import (
    async_playwright,
    TimeoutError as PWTimeout,
    Page,
    BrowserContext,
)

# ===== 基本設定 =====
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Render / 雲端常見：固定瀏覽器安裝路徑（若已存在則不覆蓋）
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/render/project/.playwright")

# 優化的 User-Agent
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


# ===== 資料類 =====
@dataclass
class ScrapingConfig:
    """抓取配置"""
    max_retries: int = 3
    retry_delay: float = 2.0
    page_timeout: int = 90_000       # 單頁導覽逾時
    wait_timeout: int = 45_000       # 等待主要元素逾時
    network_timeout: int = 15_000    # networkidle 等待逾時
    concurrency: int = 3             # 並發頁面數
    viewport: Optional[Dict[str, int]] = None

    def __post_init__(self):
        if self.viewport is None:
            self.viewport = {"width": 1280, "height": 900}


# ===== 爬蟲核心 =====
class TicketScraper:
    """OpenTix 票券剩餘量爬蟲"""

    def __init__(self, config: Optional[ScrapingConfig] = None):
        self.config = config or ScrapingConfig()
        self.browser = None
        self.context: Optional[BrowserContext] = None
        self.playwright = None

    async def __aenter__(self):
        """異步上下文管理器入口"""
        # ✅ 正確生命週期：使用 start()/stop()，避免對 Playwright 物件呼叫 __aexit__
        self.playwright = await async_playwright().start()
        await self._init_browser()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """異步上下文管理器退出"""
        await self._cleanup()
        if self.playwright:
            await self.playwright.stop()

    async def _init_browser(self):
        """初始化瀏覽器與 Context"""
        try:
            self.browser = await self.playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-extensions",
                    "--no-first-run",
                    "--disable-default-apps",
                ],
            )
            self.context = await self.browser.new_context(
                user_agent=UA,
                locale="zh-TW",
                viewport=self.config.viewport,
                ignore_https_errors=True,
                java_script_enabled=True,
                extra_http_headers={
                    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
                    "Upgrade-Insecure-Requests": "1",
                    "Cache-Control": "no-cache",
                },
            )
            await self._setup_request_interception()
        except Exception as e:
            logger.error(f"初始化瀏覽器失敗: {e}")
            raise

    async def _setup_request_interception(self):
        """攔截請求以提升效能（阻擋圖片/字體/影音）"""
        async def handle_route(route, request):
            if request.resource_type in {"image", "font", "media"}:
                await route.abort()
            else:
                await route.continue_()

        if self.context:
            await self.context.route("**/*", handle_route)

    async def _cleanup(self):
        """清理資源"""
        try:
            if self.context:
                await self.context.close()
            if self.browser:
                await self.browser.close()
        except Exception as e:
            logger.error(f"清理資源時發生錯誤: {e}")

    # ===== 對外：批次抓取 =====
    async def scrape_multiple(self, targets: List[str]) -> Dict[str, Any]:
        """
        批量抓取多個 OpenTix event URL
        回傳：
        {
          "results": [ {...}, ... ],
          "errors": [ {"url": "...", "error": "..."}, ... ],
          "summary": {"total": N, "success": x, "failed": y}
        }
        """
        if not targets:
            return {"results": [], "errors": ["no url provided"]}

        results: List[Dict[str, Any]] = []
        errors: List[Dict[str, str]] = []

        sem = asyncio.Semaphore(self.config.concurrency)

        async def worker(url: str):
            async with sem:
                try:
                    data = await self._retry_fetch(url)
                    results.append(data)
                except Exception as e:
                    msg = f"{type(e).__name__}: {e}"
                    logger.error(f"抓取 {url} 失敗: {msg}")
                    errors.append({"url": url, "error": msg})

        tasks = [worker(u.strip()) for u in targets if u and u.strip()]
        # 收集所有結果（讓 Exception 不會中斷其它任務）
        await asyncio.gather(*tasks, return_exceptions=True)

        return {
            "results": results,
            "errors": errors,
            "summary": {
                "total": len(targets),
                "success": len(results),
                "failed": len(errors),
            },
        }

    # ===== 重試機制 =====
    async def _retry_fetch(self, url: str) -> Dict[str, Any]:
        last_exc: Optional[BaseException] = None
        for attempt in range(self.config.max_retries):
            try:
                logger.info(f"嘗試抓取 {url} (第 {attempt + 1} 次)")
                return await self._fetch_single_page(url)
            except Exception as e:
                last_exc = e
                if attempt + 1 < self.config.max_retries:
                    backoff = self.config.retry_delay * (2 ** attempt)
                    logger.warning(f"抓取失敗，{backoff}秒後重試：{e}")
                    await asyncio.sleep(backoff)
                else:
                    logger.error(f"所有重試均失敗：{e}")
        assert last_exc is not None
        raise last_exc

    # ===== 單頁抓取 =====
    async def _fetch_single_page(self, url: str) -> Dict[str, Any]:
        assert self.context is not None, "Browser context 尚未初始化"
        page: Page = await self.context.new_page()
        page.set_default_timeout(self.config.page_timeout)

        try:
            # 1) 進入頁面
            await page.goto(url, wait_until="domcontentloaded", timeout=self.config.page_timeout)

            # 2) 等待主要內容 or network 閒置
            await self._wait_for_content(page)

            # 3) 嘗試處理彈窗（cookie/公告等）
            await self._handle_popups(page)

            # 4) 解析活動名稱 + 場次剩餘
            event_title = await self._get_event_title(page)
            entries = await self._parse_ticket_info(page)

            return {
                "url": url,
                "title": event_title,
                "entries": entries,
                "scraped_at": time.time(),  # 單位：秒（epoch）
                "success": True,
            }

        except Exception as e:
            logger.error(f"抓取頁面 {url} 時發生錯誤: {e}")
            return {
                "url": url,
                "title": None,
                "entries": [],
                "error": str(e),
                "success": False,
            }
        finally:
            await page.close()

    # ===== 等待內容載入 =====
    async def _wait_for_content(self, page: Page):
        """
        盡量等到場次區塊出現；否則退而求其次等 networkidle。
        OpenTix 頁面結構常見：
          <section id="purchase" ...>
            .events__content__list
            .events__list__table
        """
        try:
            await page.locator(".events__list__table").first.wait_for(
                timeout=self.config.wait_timeout
            )
        except PWTimeout:
            try:
                await page.wait_for_load_state("networkidle", timeout=self.config.network_timeout)
            except PWTimeout:
                logger.warning("頁面載入逾時（未捕捉到主要元素 / networkidle），將直接嘗試解析")

    # ===== 處理常見彈窗 =====
    async def _handle_popups(self, page: Page):
        popup_texts = ["同意", "接受", "我知道了", "OK", "關閉", "確定", "×"]
        for text in popup_texts:
            try:
                await page.get_by_role("button", name=text).first.click(timeout=600)
                await asyncio.sleep(0.4)
                break
            except Exception:
                continue

    # ===== 解析活動名稱 =====
    async def _get_event_title(self, page: Page) -> Optional[str]:
        """
        依序嘗試：
        - og:title
        - 典型 h1/h2 類選擇器
        - <title>
        """
        try:
            meta = page.locator('meta[property="og:title"]')
            if await meta.count() > 0:
                content = await meta.first.get_attribute("content")
                if content:
                    return content.strip()
        except Exception:
            pass

        for sel in [
            "h1.card__title",
            "h1.program__title",
            "h1",
            ".program__title",
        ]:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0:
                    txt = (await loc.inner_text()).strip()
                    if txt:
                        return txt
            except Exception:
                continue

        try:
            return (await page.title()).strip()
        except Exception:
            return None

    # ===== 解析票券資訊（多列） =====
    async def _parse_ticket_info(self, page: Page) -> List[Dict[str, Any]]:
        rows = page.locator(".events__list__table .column__body")
        total = await rows.count()
        logger.info(f"找到 {total} 個場次")
        entries: List[Dict[str, Any]] = []

        for i in range(total):
            try:
                row = rows.nth(i)
                entry = await self._parse_single_row(row)
                if entry:
                    entries.append(entry)
            except Exception as e:
                logger.warning(f"解析第 {i + 1 } 行失敗：{e}")
                continue

        return entries

    # ===== 解析單列 =====
    async def _parse_single_row(self, row) -> Optional[Dict[str, Any]]:
        try:
            # 場次日期
            date_txt = ""
            try:
                date_el = row.locator(".date .mr-2").first
                if await date_el.count() > 0:
                    date_txt = (await date_el.inner_text()).strip()
            except Exception:
                pass

            # 場次說明
            desc_txt = ""
            try:
                desc_el = row.locator(".date .description").first
                if await desc_el.count() > 0:
                    desc_txt = (await desc_el.inner_text()).strip()
            except Exception:
                pass

            # 剩餘票數文字
            remain_txt = ""
            try:
                remain_el = row.locator(".priceplans_wrapper .remain_infos > span").first
                if await remain_el.count() > 0:
                    remain_txt = (await remain_el.inner_text()).strip()
            except Exception:
                pass

            # 解析剩餘數
            remaining = self._extract_remaining_count(remain_txt)

            # 組合標籤
            label = f"{date_txt} {desc_txt}".strip() if (date_txt or desc_txt) else None

            # 即使 remaining 解析不到，也回傳原始字串，方便前端顯示
            return {
                "label": label,
                "remaining": remaining,              # 可能是 "123" 或 None
                "raw_remaining_text": remain_txt,    # 原始字串，例如「剩：123」
                "date": date_txt,
                "description": desc_txt,
            }

        except Exception as e:
            logger.error(f"解析行數據時發生錯誤: {e}")
            return None

    # ===== 工具：抽取剩餘數字 =====
    def _extract_remaining_count(self, text: str) -> Optional[str]:
        if not text:
            return None

        normalized = self._normalize_digits(text)

        patterns = [
            r"剩[：:]\s*([0-9,]+)",  # 剩：123 / 剩:123
            r"餘[：:]\s*([0-9,]+)",  # 餘：123
            r"還剩\s*([0-9,]+)",    # 還剩123
            r"([0-9,]+)\s*張?剩",   # 123張剩 / 123剩
            r"([0-9,]+)",           # 純數字（最後兜底）
        ]
        for pat in patterns:
            m = re.search(pat, normalized)
            if m:
                return m.group(1)
        return None

    @staticmethod
    def _normalize_digits(text: str) -> str:
        """全形數字與逗號轉半形"""
        table = str.maketrans("０１２３４５６７８９，", "0123456789,")
        return text.translate(table)


# ===== 對外主函式 =====
def _is_valid_opentix_url(url: str) -> bool:
    """驗證是否為有效的 OpenTix event URL"""
    if not url or not url.startswith(("http://", "https://")):
        return False
    try:
        parsed = urlparse(url)
        return parsed.netloc in {"opentix.life", "www.opentix.life"} and "/event/" in parsed.path
    except Exception:
        return False


async def run_once(url: Optional[str] = None, urls: Optional[str] = None) -> Dict[str, Any]:
    """
    主要對外接口：
    - 單一 url：run_once(url="...event/xxxx")
    - 多個 url（逗號分隔）：run_once(urls="url1,url2,...")
    回傳：同 scrape_multiple
    """
    # 蒐集/清理輸入
    targets: List[str] = []
    if urls:
        targets = [u.strip() for u in urls.split(",") if u and u.strip()]
    elif url:
        targets = [url.strip()]

    if not targets:
        return {"results": [], "errors": ["no url provided"]}

    # 檢核 URL
    valid_targets: List[str] = []
    invalid_urls: List[str] = []
    for t in targets:
        if _is_valid_opentix_url(t):
            valid_targets.append(t)
        else:
            invalid_urls.append(t)

    async with TicketScraper() as scraper:
        result = await scraper.scrape_multiple(valid_targets)

    # 附上不合法 URL 錯誤
    for bad in invalid_urls:
        result["errors"].append({"url": bad, "error": "Invalid OpenTix URL format"})

    return result


# ===== 向後相容接口（供 main.py 或既有程式呼叫）=====
async def scrape_status(url: str) -> Dict[str, Any]:
    """抓取單一 URL 狀態（向後相容）"""
    return await run_once(url=url)


async def scrape_event_pages(urls: Union[List[str], str]) -> Dict[str, Any]:
    """批量抓取（向後相容）；可接收 list 或逗號字串"""
    if isinstance(urls, (list, tuple)):
        joined = ",".join([str(u).strip() for u in urls if str(u).strip()])
    else:
        joined = str(urls).strip()
    return await run_once(urls=joined)


# ===== 可選：本檔案單獨執行測試 =====
if __name__ == "__main__":
    import asyncio as _asyncio

    async def _demo():
        # 在此放你的測試 URL
        test_urls = [
            # "https://www.opentix.life/event/XXXXXXXXX",
        ]
        if not test_urls:
            print("請填入測試 URL 後再執行。")
            return
        data = await run_once(urls=",".join(test_urls))
        from pprint import pprint
        pprint(data)

    _asyncio.run(_demo())
