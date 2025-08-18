# backend/scraper.py
import os
import re
import asyncio
import logging
from typing import List, Dict, Any, Optional, Union
from dataclasses import dataclass
from playwright.async_api import async_playwright, TimeoutError as PWTimeout, Page, BrowserContext

# 設定日誌
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Render/雲端常見：固定瀏覽器安裝路徑
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/render/project/.playwright")

# 優化的 User Agent 和請求頭
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

@dataclass
class ScrapingConfig:
    """抓取配置"""
    max_retries: int = 3
    retry_delay: float = 2.0
    page_timeout: int = 90_000
    wait_timeout: int = 45_000
    network_timeout: int = 15_000
    viewport: Dict[str, int] = None
    
    def __post_init__(self):
        if self.viewport is None:
            self.viewport = {"width": 1280, "height": 900}

class TicketScraper:
    """票券剩餘量爬蟲類"""
    
    def __init__(self, config: Optional[ScrapingConfig] = None):
        self.config = config or ScrapingConfig()
        self.browser = None
        self.context = None
    
    async def __aenter__(self):
        """異步上下文管理器入口"""
        self.playwright = await async_playwright().__aenter__()
        await self._init_browser()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """異步上下文管理器退出"""
        await self._cleanup()
        await self.playwright.__aexit__(exc_type, exc_val, exc_tb)
    
    async def _init_browser(self):
        """初始化瀏覽器和上下文"""
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
                ]
            )
            
            self.context = await self.browser.new_context(
                user_agent=UA,
                locale="zh-TW",
                viewport=self.config.viewport,
                ignore_https_errors=True,
                extra_http_headers={
                    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
                    "Upgrade-Insecure-Requests": "1",
                    "Cache-Control": "no-cache",
                },
                java_script_enabled=True,
            )
            
            # 設定請求攔截（可選：過濾不必要的資源）
            await self._setup_request_interception()
            
        except Exception as e:
            logger.error(f"初始化瀏覽器失敗: {e}")
            raise
    
    async def _setup_request_interception(self):
        """設定請求攔截以提高性能"""
        async def handle_route(route, request):
            # 阻止載入圖片、字體等非必要資源以提升速度
            resource_type = request.resource_type
            if resource_type in ["image", "font", "media"]:
                await route.abort()
            else:
                await route.continue_()
        
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
    
    async def scrape_multiple(self, targets: List[str]) -> Dict[str, Any]:
        """批量抓取多個 URL"""
        if not targets:
            return {"results": [], "errors": ["no url provided"]}
        
        results, errors = [], []
        
        # 使用信號量限制並發數
        semaphore = asyncio.Semaphore(3)  # 最多同時 3 個請求
        
        async def scrape_with_semaphore(url: str):
            async with semaphore:
                try:
                    data = await self._retry_fetch(url)
                    results.append(data)
                except Exception as e:
                    error_msg = f"{type(e).__name__}: {str(e)}"
                    errors.append({"url": url, "error": error_msg})
                    logger.error(f"抓取 {url} 失敗: {error_msg}")
        
        # 並發執行所有抓取任務
        tasks = [scrape_with_semaphore(url.strip()) for url in targets if url.strip()]
        await asyncio.gather(*tasks, return_exceptions=True)
        
        return {
            "results": results,
            "errors": errors,
            "summary": {
                "total": len(targets),
                "success": len(results),
                "failed": len(errors)
            }
        }
    
    async def _retry_fetch(self, url: str) -> Dict[str, Any]:
        """帶重試機制的抓取"""
        last_exception = None
        
        for attempt in range(self.config.max_retries):
            try:
                logger.info(f"嘗試抓取 {url} (第 {attempt + 1} 次)")
                return await self._fetch_single_page(url)
                
            except Exception as e:
                last_exception = e
                if attempt + 1 < self.config.max_retries:
                    wait_time = self.config.retry_delay * (2 ** attempt)  # 指數退避
                    logger.warning(f"抓取失敗，{wait_time}秒後重試: {e}")
                    await asyncio.sleep(wait_time)
                else:
                    logger.error(f"所有重試均失敗: {e}")
        
        raise last_exception
    
    async def _fetch_single_page(self, url: str) -> Dict[str, Any]:
        """抓取單一頁面"""
        page = await self.context.new_page()
        page.set_default_timeout(self.config.page_timeout)
        
        try:
            # 階段 1: 導航到頁面
            await page.goto(url, wait_until="domcontentloaded", timeout=self.config.page_timeout)
            
            # 階段 2: 等待關鍵元素載入
            await self._wait_for_content(page)
            
            # 階段 3: 處理彈窗
            await self._handle_popups(page)
            
            # 階段 4: 解析內容
            entries = await self._parse_ticket_info(page)
            
            return {
                "url": url,
                "entries": entries,
                "scraped_at": asyncio.get_event_loop().time(),
                "success": True
            }
            
        except Exception as e:
            logger.error(f"抓取頁面 {url} 時發生錯誤: {e}")
            return {
                "url": url,
                "entries": [],
                "error": str(e),
                "success": False
            }
        finally:
            await page.close()
    
    async def _wait_for_content(self, page: Page):
        """等待內容載入"""
        try:
            # 嘗試等待主要列表區塊
            await page.locator(".events__list__table").first.wait_for(
                timeout=self.config.wait_timeout
            )
        except PWTimeout:
            # 如果主要元素沒有出現，嘗試等待網路閒置
            try:
                await page.wait_for_load_state("networkidle", timeout=self.config.network_timeout)
            except PWTimeout:
                logger.warning("頁面載入逾時，但繼續解析")
    
    async def _handle_popups(self, page: Page):
        """處理各種彈窗"""
        popup_texts = ["同意", "接受", "我知道了", "OK", "關閉", "確定", "×"]
        
        for text in popup_texts:
            try:
                # 嘗試點擊按鈕
                await page.get_by_role("button", name=text).first.click(timeout=800)
                await asyncio.sleep(0.5)  # 給一點時間讓彈窗消失
                break
            except Exception:
                continue
    
    async def _parse_ticket_info(self, page: Page) -> List[Dict[str, Any]]:
        """解析票券資訊"""
        rows = page.locator(".events__list__table .column__body")
        count = await rows.count()
        entries = []
        
        logger.info(f"找到 {count} 個場次")
        
        for i in range(count):
            try:
                row = rows.nth(i)
                entry = await self._parse_single_row(row)
                if entry:
                    entries.append(entry)
            except Exception as e:
                logger.warning(f"解析第 {i+1} 行時發生錯誤: {e}")
                continue
        
        return entries
    
    async def _parse_single_row(self, row) -> Optional[Dict[str, Any]]:
        """解析單一場次行"""
        try:
            # 場次日期
            date_txt = ""
            try:
                date_element = row.locator(".date .mr-2").first
                if await date_element.count() > 0:
                    date_txt = (await date_element.inner_text()).strip()
            except Exception:
                pass
            
            # 說明文字
            desc_txt = ""
            try:
                desc_element = row.locator(".date .description").first
                if await desc_element.count() > 0:
                    desc_txt = (await desc_element.inner_text()).strip()
            except Exception:
                pass
            
            # 剩餘票數
            remain_txt = ""
            try:
                remain_element = row.locator(".priceplans_wrapper .remain_infos > span").first
                if await remain_element.count() > 0:
                    remain_txt = (await remain_element.inner_text()).strip()
            except Exception:
                pass
            
            # 解析剩餘數量
            remaining = self._extract_remaining_count(remain_txt)
            
            if remaining is not None:
                label = f"{date_txt} {desc_txt}".strip() if date_txt or desc_txt else None
                return {
                    "label": label,
                    "remaining": remaining,
                    "raw_remaining_text": remain_txt,
                    "date": date_txt,
                    "description": desc_txt
                }
            
            return None
            
        except Exception as e:
            logger.error(f"解析行數據時發生錯誤: {e}")
            return None
    
    def _extract_remaining_count(self, text: str) -> Optional[str]:
        """從文字中提取剩餘數量"""
        if not text:
            return None
        
        # 全形轉半形
        normalized = self._normalize_digits(text)
        
        # 嘗試多種模式匹配
        patterns = [
            r"剩[：:]\s*([0-9,]+)",  # 剩：123 或 剩:123
            r"餘[：:]\s*([0-9,]+)",  # 餘：123
            r"還剩\s*([0-9,]+)",    # 還剩123
            r"([0-9,]+)\s*張?剩",   # 123張剩 或 123剩
            r"([0-9,]+)"           # 純數字（最後嘗試）
        ]
        
        for pattern in patterns:
            match = re.search(pattern, normalized)
            if match:
                return match.group(1)
        
        return None
    
    def _normalize_digits(self, text: str) -> str:
        """全形數字轉半形"""
        full_to_half = str.maketrans("０１２３４５６７８９，", "0123456789,")
        return text.translate(full_to_half)

# ===== 對外主函式 =====
async def run_once(url: Optional[str] = None, urls: Optional[str] = None) -> Dict[str, Any]:
    """主要對外接口"""
    # 解析目標 URL 列表
    targets = []
    if urls:
        targets = [u.strip() for u in urls.split(",") if u.strip()]
    elif url:
        targets = [url.strip()]
    
    if not targets:
        return {"results": [], "errors": ["no url provided"]}
    
    # 驗證 URL 格式
    valid_targets = []
    invalid_urls = []
    
    for target in targets:
        if _is_valid_opentix_url(target):
            valid_targets.append(target)
        else:
            invalid_urls.append(target)
    
    # 使用優化的爬蟲類
    async with TicketScraper() as scraper:
        result = await scraper.scrape_multiple(valid_targets)
    
    # 添加無效 URL 的錯誤信息
    for invalid_url in invalid_urls:
        result["errors"].append({
            "url": invalid_url,
            "error": "Invalid OpenTix URL format"
        })
    
    return result

def _is_valid_opentix_url(url: str) -> bool:
    """驗證是否為有效的 OpenTix URL"""
    if not url.startswith(("http://", "https://")):
        return False
    
    valid_domains = ["opentix.life", "www.opentix.life"]
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        return parsed.netloc in valid_domains and "/event/" in parsed.path
    except Exception:
        return False

# ===== 向後相容的介面 =====
async def scrape_status(url: str) -> Dict[str, Any]:
    """向後相容：抓取單一 URL"""
    return await run_once(url=url)

async def scrape_event_pages(urls: Union[List[str], str]) -> Dict[str, Any]:
    """向後相容：批量抓取"""
    if isinstance(urls, (list, tuple)):
        joined = ",".join([str(u).strip() for u in urls if str(u).strip()])
    else:
        joined = str(urls).strip()
    
    return await run_once(urls=joined)
