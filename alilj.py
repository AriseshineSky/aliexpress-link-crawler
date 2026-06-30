# -*- coding: utf-8 -*-
"""
AliExpress 商品链接抓取（alilj.py）。

只抓列表页，不抓详情；从列表页 API + DOM 提取价格、评分、评论数、销量。
支持 aliexpress.us / aliexpress.com、子类目、滑块验证码自动拖动 + 手动兜底。
输出：产品链接.txt、产品列表.jsonl、Elasticsearch（ELASTICSEARCH_INDEX_URLS）
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import pyautogui
import yaml
from dotenv import load_dotenv
from elasticsearch import Elasticsearch, helpers
from playwright.async_api import BrowserContext, Page, async_playwright

BASE_DIR = Path(__file__).resolve().parent
USER_DATA_DIR = BASE_DIR / "browser"
LINKS_FILE = BASE_DIR / "产品链接.txt"
PRODUCTS_JSONL = BASE_DIR / "产品列表.jsonl"
CATEGORIES_FILE = BASE_DIR / "config" / "categories.yaml"

HEADLESS = False
CAPTCHA_WAIT_SECONDS = 120
CRAWL_SUBCATEGORIES = True
MAX_PAGES_PER_CATEGORY = 0
REQUEST_DELAY_MS = (2000, 4000)
GOTO_MAX_RETRIES = 5
GOTO_RETRY_BASE_DELAY_S = 5

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = window.chrome || { runtime: {} };
"""
CHROMIUM_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--start-maximized",
]
BROWSER_DEAD_MARKERS = (
    "Target page, context or browser has been closed",
    "TargetClosedError",
    "Browser has been closed",
)
TRANSIENT_NET_ERROR_MARKERS = (
    "net::ERR_CONNECTION_CLOSED",
    "net::ERR_CONNECTION_RESET",
    "net::ERR_CONNECTION_ABORTED",
    "net::ERR_NETWORK_CHANGED",
    "net::ERR_INTERNET_DISCONNECTED",
    "net::ERR_TIMED_OUT",
    "net::ERR_HTTP2_PROTOCOL_ERROR",
)


def site_base_from_url(url: str) -> str:
    host = urlsplit(url).netloc.lower()
    if host.endswith("aliexpress.com"):
        return "https://www.aliexpress.com"
    return "https://www.aliexpress.us"


def source_from_url(url: str) -> str:
    return "aliexpress.com" if site_base_from_url(url) == "https://www.aliexpress.com" else "aliexpress.us"


def site_host_from_url(url: str) -> str:
    return urlsplit(site_base_from_url(url)).netloc


def load_categories() -> list[tuple[str, str]]:
    with CATEGORIES_FILE.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return [(item["name"], item["url"]) for item in raw["categories"]]


def load_seen_links() -> set[str]:
    seen: set[str] = set()
    if LINKS_FILE.exists():
        with LINKS_FILE.open(encoding="utf-8") as fh:
            seen.update(line.strip() for line in fh if line.strip())
    if PRODUCTS_JSONL.exists():
        with PRODUCTS_JSONL.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                source = str(record.get("source") or "")
                product_id = str(record.get("product_id") or "")
                if source and product_id:
                    seen.add(f"{source}_{product_id}")
                elif record.get("url"):
                    seen.add(str(record["url"]))
    return seen


@dataclass
class ListingProduct:
    product_id: str
    url: str
    source: str
    title: str | None = None
    price: float | None = None
    rating: float | None = None
    reviews: int | None = None
    sold_count: int | None = None

    @property
    def dedupe_key(self) -> str:
        return f"{self.source}_{self.product_id}"


def _float_value(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = str(value).replace(",", "").replace("$", "").strip()
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _digits(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return int(digits) if digits else None


def _parse_count_text(text: str) -> int | None:
    if not text:
        return None
    upper = str(text).upper()
    match = re.search(r"(\d+(?:\.\d+)?)\s*M\+?", upper)
    if match:
        return int(float(match.group(1)) * 1_000_000)
    match = re.search(r"(\d+(?:\.\d+)?)\s*K\+?", upper)
    if match:
        return int(float(match.group(1)) * 1_000)
    return _digits(text)


def _parse_sold(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    parsed = _parse_count_text(str(value))
    return parsed if parsed is not None else _digits(value)


def _pick_rating_reviews(data: dict | None) -> tuple[float | None, int | None]:
    if not isinstance(data, dict):
        return None, None

    rating: float | None = None
    reviews: int | None = None
    for key in (
        "rating",
        "averageStar",
        "evarageStar",
        "starRating",
        "evaluationRate",
        "avgStar",
        "tradeScore",
    ):
        if key in data and data[key] is not None:
            rating = _float_value(data[key])
            if rating is not None:
                break

    for key in ("reviewCount", "reviews", "feedbackCount", "totalValidNum", "totalNum"):
        if key in data and data[key] is not None:
            reviews = _digits(data[key])
            if reviews is not None:
                break

    for nest in (
        "feedbackRating",
        "feedbackComponent",
        "productEvaluationStatistic",
        "ratingInfo",
        "reviewInfo",
        "evaluation",
    ):
        sub = data.get(nest)
        if isinstance(sub, dict):
            sub_rating, sub_reviews = _pick_rating_reviews(sub)
            if rating is None:
                rating = sub_rating
            if reviews is None:
                reviews = sub_reviews

    return rating, reviews


FEEDBACK_API = "https://feedback.aliexpress.com/pc/searchEvaluation.do"


def merge_products(products: list[ListingProduct]) -> list[ListingProduct]:
    merged: dict[str, ListingProduct] = {}
    for product in products:
        existing = merged.get(product.product_id)
        if existing is None:
            merged[product.product_id] = product
            continue
        merged[product.product_id] = ListingProduct(
            product_id=product.product_id,
            url=product.url or existing.url,
            source=product.source or existing.source,
            title=product.title or existing.title,
            price=product.price if product.price is not None else existing.price,
            rating=product.rating if product.rating is not None else existing.rating,
            reviews=product.reviews if product.reviews is not None else existing.reviews,
            sold_count=product.sold_count if product.sold_count is not None else existing.sold_count,
        )
    return list(merged.values())


def normalize_product_url(url: str) -> str | None:
    match = re.search(r"/item/(\d+)\.html", url)
    if not match:
        return None
    base = site_base_from_url(url if "://" in url else f"https:{url}")
    return f"{base}/item/{match.group(1)}.html"


def extract_product_id(url: str) -> str | None:
    match = re.search(r"/item/(\d+)\.html", url)
    return match.group(1) if match else None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_browser_dead(exc: BaseException) -> bool:
    if exc.__class__.__name__ == "TargetClosedError":
        return True
    message = str(exc)
    return any(marker in message for marker in BROWSER_DEAD_MARKERS)


def is_transient_network_error(exc: BaseException) -> bool:
    message = str(exc)
    return any(marker in message for marker in TRANSIENT_NET_ERROR_MARKERS)


def _parse_json_body(text: str) -> dict | None:
    body = text.strip()
    if not body:
        return None
    if body.startswith("mtopjsonp") or body.startswith(" mtopjsonp"):
        start = body.index("(") + 1
        end = body.rfind(")")
        body = body[start:end]
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _item_url_from_api(product_id: str, raw_url: str | None, site_base: str) -> str:
    if raw_url:
        url = raw_url if str(raw_url).startswith("http") else f"https:{raw_url}"
        clean = normalize_product_url(url)
        if clean:
            return clean
    return f"{site_base}/item/{product_id}.html"


class ListingCollector:
    """拦截 AliExpress 列表页搜索 API，补全懒加载商品。"""

    def __init__(self) -> None:
        self.search_payloads: list[dict] = []

    def clear(self) -> None:
        self.search_payloads.clear()

    async def handle_response(self, response) -> None:
        url = response.url
        if "mtop" not in url and "aer-webapi" not in url:
            return
        try:
            text = await response.text()
        except Exception:
            return
        payload = _parse_json_body(text)
        if not payload:
            return
        if any(
            marker in url
            for marker in (
                "aer-webapi/v1/search",
                "aliexpressrecommend.recommend",
                "search-pc",
            )
        ):
            self.search_payloads.append(payload)
            if len(self.search_payloads) > 30:
                del self.search_payloads[:-30]

    def extract_products(self, site_base: str) -> list[ListingProduct]:
        products: list[ListingProduct] = []
        seen: set[str] = set()
        for payload in self.search_payloads:
            for item in _extract_products_from_payload(payload, site_base):
                if item.product_id in seen:
                    continue
                seen.add(item.product_id)
                products.append(item)
        return products


def _product_from_api_fields(
    *,
    product_id: str,
    raw_url: str | None,
    site_base: str,
    title: str | None = None,
    price=None,
    rating=None,
    reviews=None,
    sold_count=None,
) -> ListingProduct | None:
    if not product_id:
        return None
    url = _item_url_from_api(product_id, raw_url, site_base)
    return ListingProduct(
        product_id=product_id,
        url=url,
        source=source_from_url(url),
        title=title,
        price=_float_value(price),
        rating=_float_value(rating),
        reviews=_digits(reviews),
        sold_count=_parse_sold(sold_count),
    )


def _extract_products_from_payload(payload: dict, site_base: str) -> list[ListingProduct]:
    products: list[ListingProduct] = []
    data = payload.get("data")
    if not isinstance(data, dict):
        return products

    products_v2 = data.get("productsFeed", {}).get("productsV2") or data.get("productsV2")
    if isinstance(products_v2, list):
        for item in products_v2:
            if not isinstance(item, dict):
                continue
            pdp = item.get("snippetContainer", {}).get("itemData", {}).get("pdpInfo", {})
            raw = pdp.get("preloadedData")
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except json.JSONDecodeError:
                    raw = {}
            if not isinstance(raw, dict):
                raw = {}
            product_id = str(item.get("productId") or pdp.get("productId") or raw.get("productId") or "")
            if not product_id or product_id == "0":
                match = re.search(r"/item/(\d+)\.html", str(pdp.get("url", "")))
                product_id = match.group(1) if match else ""
            price_info = raw.get("price") or {}
            price = price_info.get("value") if isinstance(price_info, dict) else price_info
            rating, reviews = _pick_rating_reviews(raw)
            item_rating, item_reviews = _pick_rating_reviews(item)
            pdp_rating, pdp_reviews = _pick_rating_reviews(pdp)
            product = _product_from_api_fields(
                product_id=product_id,
                raw_url=pdp.get("url"),
                site_base=site_base,
                title=raw.get("title"),
                price=price,
                rating=rating or item_rating or pdp_rating,
                reviews=reviews or item_reviews or pdp_reviews,
                sold_count=raw.get("salesCount") or raw.get("tradeCount") or raw.get("orders"),
            )
            if product:
                products.append(product)

    result = data.get("result")
    mods = data.get("mods")
    if mods is None and isinstance(result, dict):
        mods = result.get("mods")
    if isinstance(mods, dict):
        for item in mods.get("itemList", {}).get("content", []):
            if isinstance(item, dict):
                product = _product_from_recommend_item(item, site_base)
                if product:
                    products.append(product)

    for item in data.get("itemList", []):
        if isinstance(item, dict):
            product = _product_from_recommend_item(item, site_base)
            if product:
                products.append(product)

    return products


def _product_from_recommend_item(item: dict, site_base: str) -> ListingProduct | None:
    product_id = str(item.get("productId") or item.get("itemId") or item.get("id") or "")
    if not product_id:
        return None
    raw_url = item.get("productUrl") or item.get("itemUrl") or item.get("detailUrl")
    rating, reviews = _pick_rating_reviews(item)
    return _product_from_api_fields(
        product_id=product_id,
        raw_url=str(raw_url) if raw_url else None,
        site_base=site_base,
        title=item.get("title") or item.get("subject"),
        price=item.get("salePrice") or item.get("price"),
        rating=rating
        or item.get("evaluationRate")
        or item.get("starRating")
        or item.get("rating"),
        reviews=reviews or item.get("reviewCount") or item.get("feedbackCount"),
        sold_count=item.get("tradeCount") or item.get("orders") or item.get("sold"),
    )


class ElasticsearchUrlWriter:
    def __init__(self) -> None:
        load_dotenv(BASE_DIR / ".env")
        url = (os.getenv("ELASTICSEARCH_URL") or "").strip()
        self.index = (os.getenv("ELASTICSEARCH_INDEX_URLS") or "").strip()
        self.chunk_size = int(os.getenv("ELASTICSEARCH_BULK_CHUNK_SIZE", "50"))
        self.enabled = bool(url and self.index)
        self.client: Elasticsearch | None = None
        self.buffer: list[dict] = []
        self.saved = 0
        self.failed = 0

        if not self.enabled:
            print("未配置 Elasticsearch，仅写入本地文件。")
            return

        self.client = Elasticsearch(
            hosts=[url],
            timeout=60,
            retry_on_timeout=True,
            max_retries=2,
        )
        print(f"Elasticsearch 已连接，索引: {self.index}")

    def write(self, product: ListingProduct, category_name: str) -> None:
        if not self.enabled or self.client is None:
            return

        now = utc_now_iso()
        body: dict = {
            "source": product.source,
            "product_id": product.product_id,
            "url": product.url,
            "category": category_name,
            "scraped_at": now,
            "created_at": now,
            "updated_at": now,
        }
        if product.title:
            body["title"] = product.title
        if product.price is not None:
            body["price"] = product.price
        if product.rating is not None:
            body["rating"] = product.rating
        if product.reviews is not None:
            body["reviews"] = product.reviews
        if product.sold_count is not None:
            body["sold_count"] = product.sold_count

        self.buffer.append(
            {
                "_index": self.index,
                "_id": product.dedupe_key,
                "_op_type": "index",
                "_source": body,
            }
        )
        if len(self.buffer) >= self.chunk_size:
            self.flush()

    def flush(self) -> None:
        if not self.enabled or self.client is None or not self.buffer:
            return

        batch = self.buffer
        success_count, errors = helpers.bulk(
            self.client,
            batch,
            raise_on_error=False,
        )
        failed_count = len(errors) if isinstance(errors, list) else 0
        self.saved += int(success_count)
        self.failed += failed_count
        if failed_count:
            print(f"ES bulk 失败 {failed_count} 条，首条错误: {errors[0]}")
        else:
            print(f"已写入 ES {int(success_count)} 条 -> {self.index}")
        self.buffer.clear()

    def close(self) -> None:
        self.flush()
        if self.client is not None:
            self.client.close()


def wholesale_search_url(category_name: str, site_base: str) -> str:
    clean = category_name.split(" / ", 1)[-1].split(" > ", 1)[-1]
    slug = quote(clean.lower().replace("&", "and").replace(",", "").replace(" ", "-"))
    return f"{site_base}/w/wholesale-{slug}.html?SortType=total_tranpro_desc"


def build_page_url(url: str, page_no: int) -> str:
    if page_no <= 1:
        return url
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["page"] = str(page_no)
    match = re.search(r"/category/(\d+)/", parts.path)
    if match and "CatId" not in query:
        query["CatId"] = match.group(1)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def is_captcha_text(text: str) -> bool:
    lowered = text.lower()
    markers = [
        "captcha",
        "verify you are human",
        "security check",
        "slide to verify",
        "unusual traffic",
        "punish",
    ]
    return any(marker in lowered for marker in markers)


async def drag_slider_if_present(page: Page) -> bool:
    if page.is_closed():
        return False
    selector = "#nc_1_n1z"
    for frame in page.frames:
        try:
            slider = await frame.query_selector(selector)
            if not slider:
                continue
            await slider.scroll_into_view_if_needed(timeout=5000)
            box = await slider.bounding_box()
            if not box:
                continue

            start_x = box["x"] + box["width"] / 2
            start_y = box["y"] + box["height"] / 2
            drag_distance = 420

            print("发现滑块 #nc_1_n1z，尝试真实鼠标滑动结合 Playwright 拖动。")

            def move_real_mouse() -> None:
                try:
                    pyautogui.moveRel(
                        xOffset=random.randint(200, 400),
                        yOffset=random.randint(-15, 15),
                        duration=random.uniform(0.4, 0.6),
                    )
                except Exception:
                    pass

            asyncio.get_event_loop().run_in_executor(None, move_real_mouse)
            await asyncio.sleep(0.05)

            await page.mouse.move(start_x, start_y, steps=3)
            await page.mouse.down()
            steps = random.randint(4, 8)
            for i in range(1, steps + 1):
                progress = i / steps
                current_x = start_x + (drag_distance * progress)
                current_y = start_y + random.uniform(-1.0, 1.0)
                await page.mouse.move(current_x, current_y, steps=1)
                await asyncio.sleep(random.uniform(0.001, 0.003))
            await page.mouse.up()
            await page.wait_for_timeout(2000)
            return True
        except Exception as exc:
            if is_browser_dead(exc):
                raise
            print(f"拖动滑块失败，继续等待手动验证: {exc}")
            return False
    return False


async def sleep(short: bool = False) -> None:
    low, high = REQUEST_DELAY_MS
    if short:
        low, high = max(300, low // 2), max(600, high // 2)
    await asyncio.sleep(random.randint(low, high) / 1000)


async def safe_goto(page: Page, url: str) -> None:
    if page.is_closed():
        raise RuntimeError("浏览器页面已关闭，请重启爬虫")
    for attempt in range(1, GOTO_MAX_RETRIES + 1):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=90000)
            await page.wait_for_timeout(2500)
            return
        except Exception as exc:
            if is_browser_dead(exc):
                raise
            if is_transient_network_error(exc) and attempt < GOTO_MAX_RETRIES:
                wait_s = GOTO_RETRY_BASE_DELAY_S * attempt + random.uniform(0, 2)
                print(
                    f"网络异常 ({attempt}/{GOTO_MAX_RETRIES})：{exc}\n"
                    f"  {wait_s:.1f}s 后重试：{url}"
                )
                await asyncio.sleep(wait_s)
                continue
            raise


async def handle_captcha(page: Page) -> bool:
    if page.is_closed():
        raise RuntimeError("浏览器页面已关闭，请重启爬虫")
    try:
        title = await page.title()
        html = await page.content()
    except Exception as exc:
        if is_browser_dead(exc):
            raise
        return False

    slider_dragged = await drag_slider_if_present(page)
    if slider_dragged:
        title = await page.title()
        html = await page.content()
    if not slider_dragged and not is_captcha_text(title + "\n" + html):
        return False

    print(f"检测到验证/风控页面：{page.url}")
    print(f"请在打开的浏览器中手动完成验证，最多等待 {CAPTCHA_WAIT_SECONDS} 秒。")
    waited = 0
    while waited < CAPTCHA_WAIT_SECONDS:
        if page.is_closed():
            raise RuntimeError("浏览器页面已关闭，请重启爬虫")
        await drag_slider_if_present(page)
        await page.wait_for_timeout(2000)
        waited += 2
        title = await page.title()
        html = await page.content()
        if not is_captcha_text(title + "\n" + html):
            print("验证已通过，继续抓取。")
            return True
    print("等待结束，页面可能仍在验证状态。")
    return True


async def scroll_listing_page(page: Page) -> None:
    await page.evaluate(
        """
        async () => {
          for (let i = 0; i < 12; i++) {
            window.scrollBy(0, Math.max(700, window.innerHeight * 0.85));
            await new Promise((resolve) => setTimeout(resolve, 450));
          }
          window.scrollTo(0, 0);
        }
        """
    )
    await page.wait_for_timeout(3500)


async def enrich_rating_reviews(page: Page, products: list[ListingProduct]) -> None:
    """US 列表卡片常不显示评论数，用 feedback API 补全 rating / reviews。"""
    for product in products:
        if product.rating is not None and product.reviews is not None:
            continue
        try:
            response = await page.request.get(
                FEEDBACK_API,
                params={
                    "productId": product.product_id,
                    "page": "1",
                    "pageSize": "1",
                    "filter": "all",
                    "sort": "complex_default",
                },
                timeout=15000,
            )
            if not response.ok:
                continue
            payload = await response.json()
            data = payload.get("data") if isinstance(payload, dict) else {}
            if not isinstance(data, dict):
                continue
            stats = data.get("productEvaluationStatistic")
            rating, reviews = _pick_rating_reviews(stats if isinstance(stats, dict) else {})
            if product.rating is None and rating is not None:
                product.rating = rating
            if product.reviews is None:
                if reviews is None:
                    reviews = _digits(data.get("totalNum"))
                if reviews is not None:
                    product.reviews = reviews
        except Exception:
            continue
        await asyncio.sleep(0.12)


async def extract_products(page: Page, site_base: str, collector: ListingCollector) -> list[ListingProduct]:
    await scroll_listing_page(page)

    products = collector.extract_products(site_base)

    raw_items = await page.evaluate(
        """
        () => {
          const results = [];
          const seen = new Set();
          const selectors = [
            '[class*="card-out-wrapper"] a[href*="/item/"]',
            '[class*="search-card"] a[href*="/item/"]',
            'a[href*="/item/"]',
          ];
          for (const selector of selectors) {
            for (const anchor of document.querySelectorAll(selector)) {
              const href = anchor.href || anchor.getAttribute('href') || '';
              const match = href.match(/\\/item\\/(\\d+)\\.html/);
              if (!match) continue;
              const productId = match[1];
              if (seen.has(productId)) continue;
              seen.add(productId);

              const card = anchor.closest('[class*="card"], [class*="item"], [class*="product"], li, div');
              let price = null;
              let rating = null;
              let reviews = null;
              let sold = null;
              let title = null;

              if (card) {
                const text = card.innerText || '';
                const titleNode = card.querySelector('h1,h2,h3,[class*="title"]');
                title = titleNode ? (titleNode.innerText || '').trim() : null;
                const priceMatch = text.match(/\\$\\s?(\\d+(?:\\.\\d+)?)/);
                if (priceMatch) price = parseFloat(priceMatch[1]);

                const ratingAfterDiscount = text.match(/-\\d+%\\s*\\n\\s*(\\d\\.\\d)\\s*\\n/);
                if (ratingAfterDiscount) {
                  rating = parseFloat(ratingAfterDiscount[1]);
                }
                if (rating === null) {
                  const ratingLine = text.match(/(?:^|\\n)\\s*(\\d\\.\\d)\\s*(?:\\n|$)/m);
                  if (ratingLine) rating = parseFloat(ratingLine[1]);
                }
                if (rating === null) {
                  const ratingWithParen = text.match(/(\\d\\.\\d)\\s*\\(/);
                  if (ratingWithParen) rating = parseFloat(ratingWithParen[1]);
                }

                const reviewsText = text.match(/(\\d[\\d,]*)\\s+reviews?\\b/i)
                  || text.match(/(\\d[\\d,]*)\\s+ratings?\\b/i);
                if (reviewsText) {
                  reviews = parseInt(reviewsText[1].replace(/,/g, ''), 10);
                } else {
                  const reviewsParen = text.match(/\\((\\d[\\d,]*)\\)/);
                  if (reviewsParen) reviews = parseInt(reviewsParen[1].replace(/,/g, ''), 10);
                }

                const soldMatch = text.match(/(\\d[\\d,+KkMm.]*)\\s+(?:sold|sales|orders)/i);
                if (soldMatch) sold = soldMatch[1];
              }

              results.push({
                product_id: productId,
                url: href.split('?')[0],
                title,
                price,
                rating,
                reviews,
                sold_count: sold,
              });
            }
          }
          return results;
        }
        """
    )
    for item in raw_items:
        product_id = str(item.get("product_id") or "")
        if not product_id:
            continue
        url = normalize_product_url(str(item.get("url") or ""))
        if not url:
            url = f"{site_base}/item/{product_id}.html"
        if site_base not in url:
            url = f"{site_base}/item/{product_id}.html"
        sold = item.get("sold_count")
        products.append(
            ListingProduct(
                product_id=product_id,
                url=url,
                source=source_from_url(url),
                title=item.get("title") or None,
                price=_float_value(item.get("price")),
                rating=_float_value(item.get("rating")),
                reviews=_digits(item.get("reviews")),
                sold_count=_parse_count_text(str(sold)) if sold else None,
            )
        )

    products = merge_products(products)
    await enrich_rating_reviews(page, products)
    return products


async def discover_subcategories(page: Page, category_name: str, category_url: str) -> list[tuple[str, str]]:
    parent_match = re.search(r"/category/(\d+)/", category_url)
    parent_id = parent_match.group(1) if parent_match else ""
    site_host = site_host_from_url(category_url)
    await safe_goto(page, category_url)
    await handle_captcha(page)
    await sleep()

    raw_items = await page.evaluate(
        """
        ({ parentId, siteHost }) => {
          const results = [];
          const seen = new Set();
          const selectors = [
            'div[class*="refine"] a[href*="/category/"]',
            'div[class*="category"] a[href*="/category/"]',
            'nav a[href*="/category/"]',
            'a[href*="/category/"]',
          ];
          for (const selector of selectors) {
            for (const anchor of document.querySelectorAll(selector)) {
              const href = anchor.href || anchor.getAttribute('href') || '';
              if (siteHost && !href.includes(siteHost)) continue;
              const match = href.match(/\\/category\\/(\\d+)\\/[^?]+\\.html/);
              if (!match) continue;
              const catId = match[1];
              if (parentId && catId === parentId) continue;
              if (seen.has(catId)) continue;
              const name = (anchor.innerText || anchor.textContent || '').trim();
              if (!name || name.length < 2 || name.length > 80) continue;
              const lowered = name.toLowerCase();
              if (['home', 'all categories', 'see all'].includes(lowered)) continue;
              seen.add(catId);
              results.push({
                name,
                url: href.split('?')[0] + '?SortType=total_tranpro_desc',
              });
            }
            if (results.length) break;
          }
          return results;
        }
        """,
        {"parentId": parent_id, "siteHost": site_host},
    )

    subcategories: list[tuple[str, str]] = []
    for item in raw_items or []:
        subcategories.append((f"{category_name} > {item['name']}", item["url"]))
    return subcategories


def save_new_products(
    products: list[ListingProduct],
    seen_keys: set[str],
    category_name: str,
    es_writer: ElasticsearchUrlWriter | None = None,
) -> int:
    new_count = 0
    LINKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LINKS_FILE.open("a", encoding="utf-8") as links_fh, PRODUCTS_JSONL.open(
        "a", encoding="utf-8"
    ) as jsonl_fh:
        for product in products:
            if product.dedupe_key in seen_keys or product.url in seen_keys:
                continue
            seen_keys.add(product.dedupe_key)
            seen_keys.add(product.url)
            links_fh.write(product.url + "\n")
            record = {
                "source": product.source,
                "product_id": product.product_id,
                "url": product.url,
                "category": category_name,
                "title": product.title,
                "price": product.price,
                "rating": product.rating,
                "reviews": product.reviews,
                "sold_count": product.sold_count,
                "scraped_at": utc_now_iso(),
            }
            jsonl_fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            if es_writer is not None:
                es_writer.write(product, category_name)
            new_count += 1
    return new_count


async def crawl_category(
    page: Page,
    category_name: str,
    category_url: str,
    seen_links: set[str],
    collector: ListingCollector,
    es_writer: ElasticsearchUrlWriter | None = None,
) -> int:
    total_new = 0
    site_base = site_base_from_url(category_url)
    start_urls = [category_url]
    if " > " not in category_name:
        start_urls.append(wholesale_search_url(category_name, site_base))

    print(f"\n类目：{category_name}")
    category_seen_ids: set[str] = set()

    for start_url in start_urls:
        page_no = 1
        while True:
            if MAX_PAGES_PER_CATEGORY > 0 and page_no > MAX_PAGES_PER_CATEGORY:
                break

            page_url = build_page_url(start_url, page_no)
            print(f"打开第 {page_no} 页：{page_url}")
            collector.clear()
            await safe_goto(page, page_url)
            await handle_captcha(page)
            await sleep(short=page_no > 1)

            products = await extract_products(page, site_base, collector)
            current_ids = {p.product_id for p in products}
            fresh_in_category = current_ids - category_seen_ids
            new_count = save_new_products(products, seen_links, category_name, es_writer)
            if es_writer is not None:
                es_writer.flush()
            total_new += new_count
            with_metrics = sum(
                1
                for p in products
                if p.rating is not None and p.reviews is not None
            )
            print(
                f"第 {page_no} 页：发现 {len(products)} 个商品（{with_metrics} 个含评分+评论数），"
                f"本类目新商品 {len(fresh_in_category)} 个，保存新增 {new_count} 个"
            )

            if not current_ids:
                print(f"第 {page_no} 页没有数据，判定 {category_name} 到最后一页。")
                break
            if page_no > 1 and not fresh_in_category:
                print(f"第 {page_no} 页和前面页面数据重复，判定 {category_name} 没有更多页。")
                break

            category_seen_ids.update(current_ids)
            page_no += 1
            await sleep()

        if category_seen_ids:
            return total_new

    return total_new


async def create_browser(playwright) -> tuple[BrowserContext, Page, ListingCollector]:
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    collector = ListingCollector()
    context = await playwright.chromium.launch_persistent_context(
        user_data_dir=str(USER_DATA_DIR),
        headless=HEADLESS,
        user_agent=USER_AGENT,
        locale="en-US",
        viewport=None,
        no_viewport=True,
        args=CHROMIUM_ARGS,
        ignore_default_args=["--enable-automation"],
    )
    await context.add_init_script(STEALTH_SCRIPT)
    page = context.pages[0] if context.pages else await context.new_page()

    async def on_response(response) -> None:
        await collector.handle_response(response)

    page.on("response", on_response)
    return context, page, collector


async def close_browser(context: BrowserContext | None) -> None:
    if context is None:
        return
    try:
        await context.close()
    except Exception:
        pass


async def main_async() -> None:
    categories = load_categories()
    print("=" * 60)
    print("AliExpress 商品链接抓取 (alilj.py)")
    print("=" * 60)
    print(f"浏览器状态目录: {USER_DATA_DIR}")
    print(f"商品链接文件: {LINKS_FILE}")
    print(f"商品列表文件: {PRODUCTS_JSONL}")
    print(f"类目数量: {len(categories)}（含 US / COM）")
    print(f"子类目发现: {'开启' if CRAWL_SUBCATEGORIES else '关闭'}")
    print()

    seen_links = load_seen_links()
    total_new = 0
    es_writer = ElasticsearchUrlWriter()

    async with async_playwright() as playwright:
        context: BrowserContext | None = None
        page: Page | None = None
        collector: ListingCollector | None = None
        try:
            for category_name, category_url in categories:
                targets = [(category_name, category_url)]
                if CRAWL_SUBCATEGORIES:
                    for attempt in range(2):
                        try:
                            if context is None or page is None or page.is_closed():
                                await close_browser(context)
                                context, page, collector = await create_browser(playwright)
                            subs = await discover_subcategories(page, category_name, category_url)
                            if subs:
                                print(f"发现 {len(subs)} 个子类目：{category_name}")
                                targets.extend(subs)
                            break
                        except Exception as exc:
                            if is_browser_dead(exc) and attempt == 0:
                                print(f"子类目发现时浏览器异常，重启：{exc}")
                                await close_browser(context)
                                context, page, collector = None, None, None
                                await asyncio.sleep(3)
                                continue
                            print(f"子类目发现失败 {category_name}: {exc}")
                            break

                for target_name, target_url in targets:
                    for attempt in range(2):
                        try:
                            if context is None or page is None or page.is_closed():
                                await close_browser(context)
                                context, page, collector = await create_browser(playwright)
                            assert collector is not None
                            total_new += await crawl_category(
                                page, target_name, target_url, seen_links, collector, es_writer
                            )
                            break
                        except Exception as exc:
                            if is_browser_dead(exc) and attempt == 0:
                                print(f"抓取时浏览器异常，重启：{exc}")
                                await close_browser(context)
                                context, page, collector = None, None, None
                                await asyncio.sleep(3)
                                continue
                            raise
        finally:
            await close_browser(context)
            es_writer.close()

    print("\n完成。")
    print(f"累计商品数: {len(seen_links)}")
    print(f"本次新增: {total_new}")
    print(f"链接文件: {LINKS_FILE}")
    print(f"列表文件: {PRODUCTS_JSONL}")
    if es_writer.enabled:
        print(f"ES 索引: {es_writer.index}")
        print(f"ES 本次写入: {es_writer.saved}，失败: {es_writer.failed}")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
