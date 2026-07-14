# -*- coding: utf-8 -*-
"""
AliExpress 商品链接抓取（alilj.py）。

只抓列表页，不抓详情；从列表页 API + DOM 提取价格、评分、评论数、销量。
首页分类入口为 /p/calp-plus/?categoryTab=...，商品靠无限下滑加载（非 ?page=N）。
默认只抓 aliexpress.us；支持 calp 子类目点击、滑块验证码自动拖动 + 手动兜底。
默认偏提速：短间隔、滚动等列表 API、feedback 补全新商品评分、calp 页内点子类。
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
MAX_SUBCATEGORY_DEPTH = 5
MAX_PAGES_PER_CATEGORY = 0  # 兼容旧环境变量；>0 时作为最大滚动轮数
MAX_SCROLL_ROUNDS = 0  # 0 = 不限制，直到连续无新商品
CONSECUTIVE_DUPLICATE_SCROLLS_TO_STOP = 10
REQUEST_DELAY_MS = (600, 1200)  # 原 2000–4000，默认提速
GOTO_SETTLE_MS = 800  # safe_goto 后固定等待，原 2500
SCROLL_API_TIMEOUT_MS = 3500  # 滚动后等列表 API
SCROLL_FALLBACK_MS = 1200  # API 超时回退等待，原固定 2200
SCROLL_AFTER_API_MS = 250
FIRST_ROUND_SETTLE_MS = 600  # 首轮原 1500
CALP_CLICK_SETTLE_MS = 1000  # 点 lv3 后等待，原 2500
CALP_RELOAD_EACH_LV3 = False  # True=每个子类目整页重开（更稳更慢）
ENRICH_MODE = "new"  # off | new | all（默认补全新商品评分/评论）
ENRICH_CONCURRENCY = 8
ENRICH_DELAY_MS = 30
GOTO_MAX_RETRIES = 5
GOTO_RETRY_BASE_DELAY_S = 5

LIST_API_URL_MARKERS = (
    "aer-webapi/v1/search",
    "aliexpressrecommend.recommend",
    "aliexpressseorecommend.recommend",
    "seorecommend",
    "search-pc",
)

load_dotenv(BASE_DIR / ".env")
if categories_file := (os.getenv("CATEGORIES_FILE") or "").strip():
    CATEGORIES_FILE = Path(categories_file)
    if not CATEGORIES_FILE.is_absolute():
        CATEGORIES_FILE = BASE_DIR / CATEGORIES_FILE
if max_pages := (os.getenv("MAX_PAGES_PER_CATEGORY") or "").strip():
    MAX_PAGES_PER_CATEGORY = int(max_pages)
    if MAX_PAGES_PER_CATEGORY > 0 and not (os.getenv("MAX_SCROLL_ROUNDS") or "").strip():
        MAX_SCROLL_ROUNDS = MAX_PAGES_PER_CATEGORY
if max_scrolls := (os.getenv("MAX_SCROLL_ROUNDS") or "").strip():
    MAX_SCROLL_ROUNDS = int(max_scrolls)
if (os.getenv("CRAWL_SUBCATEGORIES") or "").strip().lower() in {"0", "false", "no"}:
    CRAWL_SUBCATEGORIES = False
if (os.getenv("HEADLESS") or "").strip().lower() in {"1", "true", "yes"}:
    HEADLESS = True
if delay_ms := (os.getenv("REQUEST_DELAY_MS") or "").strip():
    low, high = delay_ms.split(",", 1)
    REQUEST_DELAY_MS = (int(low), int(high))
if dup_stop := (os.getenv("CONSECUTIVE_DUPLICATE_SCROLLS_TO_STOP") or "").strip():
    CONSECUTIVE_DUPLICATE_SCROLLS_TO_STOP = int(dup_stop)
if goto_settle := (os.getenv("GOTO_SETTLE_MS") or "").strip():
    GOTO_SETTLE_MS = int(goto_settle)
if scroll_api := (os.getenv("SCROLL_API_TIMEOUT_MS") or "").strip():
    SCROLL_API_TIMEOUT_MS = int(scroll_api)
if scroll_fallback := (os.getenv("SCROLL_FALLBACK_MS") or "").strip():
    SCROLL_FALLBACK_MS = int(scroll_fallback)
if (os.getenv("CALP_RELOAD_EACH_LV3") or "").strip().lower() in {"1", "true", "yes"}:
    CALP_RELOAD_EACH_LV3 = True
if enrich_mode := (os.getenv("ENRICH_MODE") or "").strip().lower():
    if enrich_mode in {"off", "new", "all"}:
        ENRICH_MODE = enrich_mode
elif (os.getenv("ENRICH_RATING_REVIEWS") or "").strip().lower() in {"1", "true", "yes"}:
    ENRICH_MODE = "new"
elif (os.getenv("ENRICH_RATING_REVIEWS") or "").strip().lower() in {"0", "false", "no"}:
    ENRICH_MODE = "off"
if enrich_conc := (os.getenv("ENRICH_CONCURRENCY") or "").strip():
    ENRICH_CONCURRENCY = max(1, int(enrich_conc))
if enrich_delay := (os.getenv("ENRICH_DELAY_MS") or "").strip():
    ENRICH_DELAY_MS = max(0, int(enrich_delay))

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
FRAME_TRANSIENT_ERROR_MARKERS = (
    "Frame was detached",
    "frame was detached",
    "Execution context was destroyed",
    "Cannot find context with specified id",
    "Frame.querySelector",
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


def category_path_depth(category_name: str) -> int:
    """Return hierarchy depth after the site prefix, e.g. 'US / A > B > C' -> 3."""
    path = category_name.split(" / ", 1)[-1]
    return path.count(" > ") + 1


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
        key = product.product_id or product.dedupe_key
        existing = merged.get(key)
        if existing is None:
            merged[key] = product
            continue
        preferred_url = product.url if site_base_from_url(product.url).endswith(".us") else existing.url
        if not preferred_url:
            preferred_url = existing.url or product.url
        merged[key] = ListingProduct(
            product_id=product.product_id,
            url=preferred_url or product.url or existing.url,
            source=source_from_url(preferred_url or product.url or existing.url),
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


def is_browser_dead(exc: BaseException, *, page: Page | None = None) -> bool:
    is_target_closed = exc.__class__.__name__ == "TargetClosedError"
    message = str(exc)
    looks_closed = is_target_closed or any(
        marker in message for marker in BROWSER_DEAD_MARKERS
    )
    if not looks_closed:
        return False
    # Captcha iframes often detach while the page is still alive.
    if page is not None and not page.is_closed():
        return False
    return True


def is_transient_network_error(exc: BaseException) -> bool:
    message = str(exc)
    return any(marker in message for marker in TRANSIENT_NET_ERROR_MARKERS)


def is_transient_frame_error(exc: BaseException) -> bool:
    message = str(exc)
    return any(marker in message for marker in FRAME_TRANSIENT_ERROR_MARKERS)


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
    """拦截 AliExpress 列表/推荐 API，补全无限滚动懒加载商品。"""

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
        if any(marker in url.lower() for marker in LIST_API_URL_MARKERS):
            self.search_payloads.append(payload)
            if len(self.search_payloads) > 40:
                del self.search_payloads[:-40]

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
        source=source_from_url(site_base),
        title=_title_value(title),
        price=_float_value(price),
        rating=_float_value(rating),
        reviews=_digits(reviews),
        sold_count=_parse_sold(sold_count),
    )


def _title_value(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, dict):
        for key in ("displayTitle", "title", "seoTitle", "subject", "name"):
            nested = _title_value(value.get(key))
            if nested:
                return nested
    return None


def _pick_sold(data: dict | None) -> int | None:
    if not isinstance(data, dict):
        return None
    for key in (
        "realTradeCount",
        "real_trade_count",
        "salesCount",
        "tradeCount",
        "sold",
        "orders",
        "tradeDesc",
        "soldCount",
        "itemSold",
    ):
        if key in data and data[key] is not None:
            sold = _parse_sold(data[key])
            if sold is not None:
                return sold
    for nest in ("trade", "sales", "trace"):
        sub = data.get(nest)
        if isinstance(sub, dict):
            sold = _pick_sold(sub)
            if sold is not None:
                return sold
            ut = sub.get("utLogMap")
            if isinstance(ut, dict):
                sold = _pick_sold(ut)
                if sold is not None:
                    return sold
    return None


def _pick_price(data: dict | None):
    if not isinstance(data, dict):
        return None
    for key in ("salePrice", "price", "minPrice", "actSkuCalPrice", "skuVal"):
        if key in data and data[key] is not None:
            value = data[key]
            if isinstance(value, dict):
                for nested_key in ("value", "cent", "formattedPrice", "amount"):
                    if nested_key in value:
                        return value.get(nested_key)
                return value.get("salePrice") or value.get("price")
            return value
    prices = data.get("prices")
    if isinstance(prices, dict):
        return _pick_price(prices)
    return None


def _iter_product_item_dicts(node, out: list[dict], *, depth: int = 0) -> None:
    """Recursively collect list-card product dicts from nested AE payloads."""
    if depth > 12:
        return
    if isinstance(node, dict):
        content = node.get("content")
        if (
            isinstance(content, list)
            and content
            and isinstance(content[0], dict)
            and any(
                key in content[0]
                for key in ("productId", "itemId", "evaluation", "trade", "productDetailUrl")
            )
        ):
            out.extend(item for item in content if isinstance(item, dict))
        products_v2 = node.get("productsV2")
        if isinstance(products_v2, list):
            out.extend(item for item in products_v2 if isinstance(item, dict))
        item_list = node.get("itemList")
        if isinstance(item_list, list):
            out.extend(item for item in item_list if isinstance(item, dict))
        for value in node.values():
            if isinstance(value, (dict, list)):
                _iter_product_item_dicts(value, out, depth=depth + 1)
    elif isinstance(node, list):
        for value in node:
            if isinstance(value, (dict, list)):
                _iter_product_item_dicts(value, out, depth=depth + 1)


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
                title=_title_value(raw.get("title")),
                price=price,
                rating=rating or item_rating or pdp_rating,
                reviews=reviews or item_reviews or pdp_reviews,
                sold_count=_pick_sold(raw) or _pick_sold(item) or _pick_sold(pdp),
            )
            if product:
                products.append(product)

    raw_items: list[dict] = []
    _iter_product_item_dicts(data, raw_items)
    seen_ids: set[str] = set()
    for item in raw_items:
        product = _product_from_recommend_item(item, site_base)
        if not product or product.product_id in seen_ids:
            continue
        seen_ids.add(product.product_id)
        products.append(product)

    return products


def _product_from_recommend_item(item: dict, site_base: str) -> ListingProduct | None:
    product_id = str(item.get("productId") or item.get("itemId") or item.get("id") or "")
    if not product_id or product_id == "0":
        return None
    raw_url = (
        item.get("productDetailUrl")
        or item.get("productUrl")
        or item.get("itemUrl")
        or item.get("detailUrl")
    )
    rating, reviews = _pick_rating_reviews(item)
    ut = item.get("trace", {}).get("utLogMap") if isinstance(item.get("trace"), dict) else None
    if isinstance(ut, dict):
        ut_rating, ut_reviews = _pick_rating_reviews(ut)
        rating = rating or ut_rating or _float_value(ut.get("star_rating"))
        reviews = reviews or ut_reviews
    return _product_from_api_fields(
        product_id=product_id,
        raw_url=str(raw_url) if raw_url else None,
        site_base=site_base,
        title=_title_value(item.get("title") or item.get("subject") or item.get("productTitle")),
        price=_pick_price(item),
        rating=rating,
        reviews=reviews,
        sold_count=_pick_sold(item),
    )


class ElasticsearchUrlWriter:
    def __init__(self) -> None:
        load_dotenv(BASE_DIR / ".env")
        url = (os.getenv("ELASTICSEARCH_URL") or "").strip()
        self.index = (os.getenv("ELASTICSEARCH_INDEX_URLS") or "").strip()
        placeholder = (not url) or ("@host:" in url) or url.rstrip("/").endswith("://host:9200")
        self.chunk_size = int(os.getenv("ELASTICSEARCH_BULK_CHUNK_SIZE", "50"))
        self.enabled = bool(url and self.index and not placeholder)
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
        fields: dict = {
            "source": product.source,
            "product_id": product.product_id,
            "url": product.url,
            "category": category_name,
            "scraped_at": now,
            "updated_at": now,
        }
        if product.title:
            fields["title"] = product.title
        if product.price is not None:
            fields["price"] = product.price
        # Overwrite metric fields when this crawl captured them.
        if product.rating is not None:
            fields["rating"] = product.rating
        if product.reviews is not None:
            fields["reviews"] = product.reviews
        if product.sold_count is not None:
            fields["sold_count"] = product.sold_count

        upsert = dict(fields)
        upsert["created_at"] = now

        self.buffer.append(
            {
                "_index": self.index,
                "_id": product.dedupe_key,
                "_op_type": "update",
                "doc": fields,
                "upsert": upsert,
            }
        )
        if len(self.buffer) >= self.chunk_size:
            self.flush()

    def flush(self) -> None:
        if not self.enabled or self.client is None or not self.buffer:
            return

        batch = self.buffer
        try:
            success_count, errors = helpers.bulk(
                self.client,
                batch,
                raise_on_error=False,
            )
        except Exception as exc:
            self.failed += len(batch)
            self.buffer.clear()
            print(f"ES bulk 失败（已跳过 {len(batch)} 条）：{exc}")
            return
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


async def sniff_captcha_signals(page: Page) -> bool:
    """轻量风控检测：title / URL / 正文前 2k，避免每轮 page.content()。"""
    if page.is_closed():
        return False
    url = (page.url or "").lower()
    if any(
        marker in url
        for marker in ("punish", "captcha", "_____tmd_____", "/login", "security")
    ):
        return True
    try:
        title = await page.title()
    except Exception as exc:
        if is_browser_dead(exc, page=page):
            raise
        return False
    if is_captcha_text(title):
        return True
    try:
        snippet = await page.evaluate(
            "() => ((document.body && document.body.innerText) || '').slice(0, 2000)"
        )
    except Exception as exc:
        if is_browser_dead(exc, page=page):
            raise
        return False
    return is_captcha_text(str(snippet or ""))


def is_list_api_response(response) -> bool:
    url = (response.url or "").lower()
    if "mtop" not in url and "aer-webapi" not in url:
        return False
    return any(marker in url for marker in LIST_API_URL_MARKERS)


async def drag_slider_if_present(page: Page) -> bool:
    if page.is_closed():
        return False
    selector = "#nc_1_n1z"
    for frame in list(page.frames):
        if frame.is_detached():
            continue
        try:
            slider = await frame.query_selector(selector)
        except Exception as exc:
            if is_browser_dead(exc, page=page):
                raise
            if is_transient_frame_error(exc):
                continue
            print(f"查找滑块时跳过异常 frame: {exc}")
            continue
        if not slider:
            continue
        try:
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
            if is_browser_dead(exc, page=page):
                raise
            if is_transient_frame_error(exc):
                continue
            print(f"拖动滑块失败，继续等待手动验证: {exc}")
            continue
    return False


async def sleep(short: bool = False) -> None:
    low, high = REQUEST_DELAY_MS
    if short:
        low, high = max(150, low // 2), max(300, high // 2)
    await asyncio.sleep(random.randint(low, high) / 1000)


async def safe_goto(page: Page, url: str) -> None:
    if page.is_closed():
        raise RuntimeError("浏览器页面已关闭，请重启爬虫")
    for attempt in range(1, GOTO_MAX_RETRIES + 1):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=90000)
            if GOTO_SETTLE_MS > 0:
                await page.wait_for_timeout(GOTO_SETTLE_MS)
            return
        except Exception as exc:
            if is_browser_dead(exc, page=page):
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
    await dismiss_popups(page)

    slider_dragged = await drag_slider_if_present(page)
    if not slider_dragged and not await sniff_captcha_signals(page):
        return False

    print(f"检测到验证/风控页面：{page.url}")
    print(f"请在打开的浏览器中手动完成验证，最多等待 {CAPTCHA_WAIT_SECONDS} 秒。")
    waited = 0
    while waited < CAPTCHA_WAIT_SECONDS:
        if page.is_closed():
            raise RuntimeError("浏览器页面已关闭，请重启爬虫")
        await drag_slider_if_present(page)
        try:
            await page.wait_for_timeout(2000)
        except Exception as exc:
            if is_browser_dead(exc, page=page):
                raise
            continue
        waited += 2
        if not await sniff_captcha_signals(page):
            print("验证已通过，继续抓取。")
            return True
    print("等待结束，页面可能仍在验证状态。")
    return True


async def dismiss_popups(page: Page) -> None:
    """Close login / promo overlays that block scrolling."""
    if page.is_closed():
        return
    for sel in (
        ".pop-close-btn",
        ".batman-dialog-close",
        "[class*='batman-dialog'] [class*='close']",
        "button[aria-label='Close']",
        "button[aria-label='close']",
    ):
        try:
            loc = page.locator(sel).first
            if await loc.count() and await loc.is_visible():
                await loc.click(timeout=1500, force=True)
                await page.wait_for_timeout(200)
        except Exception:
            continue
    try:
        await page.evaluate(
            """
            () => {
              for (const el of [...document.querySelectorAll('div,section')]) {
                const text = el.innerText || '';
                if (!text.includes('Register/Sign in')) continue;
                const r = el.getBoundingClientRect();
                if (r.width < 300 || r.height < 200) continue;
                let p = el;
                for (let i = 0; i < 6 && p; i++) {
                  const style = getComputedStyle(p);
                  const box = p.getBoundingClientRect();
                  if (
                    style.position === 'fixed' ||
                    (box.width >= window.innerWidth * 0.7 &&
                      box.height >= window.innerHeight * 0.5)
                  ) {
                    p.remove();
                    break;
                  }
                  p = p.parentElement;
                }
              }
              document.body.style.overflow = 'auto';
            }
            """
        )
    except Exception:
        pass


def is_calp_url(url: str) -> bool:
    return "/p/calp-plus/" in url or "categoryTab=" in url


async def scroll_listing_page(page: Page) -> None:
    """One user-like scroll step; prefer waiting for list API over fixed delay."""
    await dismiss_popups(page)
    try:
        async with page.expect_response(is_list_api_response, timeout=SCROLL_API_TIMEOUT_MS):
            await page.evaluate(
                "window.scrollBy(0, Math.max(1000, window.innerHeight * 0.9))"
            )
        if SCROLL_AFTER_API_MS > 0:
            await page.wait_for_timeout(SCROLL_AFTER_API_MS)
        return
    except Exception as exc:
        if is_browser_dead(exc, page=page):
            raise
        # Timeout：滚动多半已完成且已等过 SCROLL_API_TIMEOUT_MS，无需再叠长等待。
        name = type(exc).__name__
        if "Timeout" in name:
            return
    if SCROLL_FALLBACK_MS > 0:
        await page.wait_for_timeout(SCROLL_FALLBACK_MS)


async def _enrich_one_product(page: Page, product: ListingProduct) -> None:
    if product.rating is not None and product.reviews is not None:
        return
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
            return
        payload = await response.json()
        data = payload.get("data") if isinstance(payload, dict) else {}
        if not isinstance(data, dict):
            return
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
        return
    if ENRICH_DELAY_MS > 0:
        await asyncio.sleep(ENRICH_DELAY_MS / 1000)


async def enrich_rating_reviews(page: Page, products: list[ListingProduct]) -> None:
    """US 列表卡片常不显示评论数，用 feedback API 补全 rating / reviews。"""
    if ENRICH_MODE == "off" or not products:
        return
    pending = [
        p
        for p in products
        if p.rating is None or p.reviews is None
    ]
    if not pending:
        return
    sem = asyncio.Semaphore(ENRICH_CONCURRENCY)

    async def _run(product: ListingProduct) -> None:
        async with sem:
            await _enrich_one_product(page, product)

    await asyncio.gather(*(_run(p) for p in pending))


async def extract_products(page: Page, site_base: str, collector: ListingCollector) -> list[ListingProduct]:
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
                source=source_from_url(site_base),
                title=item.get("title") or None,
                price=_float_value(item.get("price")),
                rating=_float_value(item.get("rating")),
                reviews=_digits(item.get("reviews")),
                sold_count=_parse_count_text(str(sold)) if sold else None,
            )
        )

    products = merge_products(products)
    return products


def category_id_from_url(category_url: str) -> str:
    match = re.search(r"/category/(\d+)/", category_url)
    return match.group(1) if match else ""


async def list_calp_lv3_names(page: Page) -> list[str]:
    names = await page.evaluate(
        """
        () => {
          const out = [];
          const seen = new Set();
          for (const el of document.querySelectorAll('[class*="lv3Category"]')) {
            const box = el.closest('[class*="lv3CategoryBox"]') || el;
            const text = (box.innerText || '').trim().replace(/\\s+/g, ' ');
            const name = text.split('\\n').map(s => s.trim()).filter(Boolean).pop() || text;
            if (!name || name.length < 2 || name.length > 60) continue;
            const key = name.toLowerCase();
            if (seen.has(key)) continue;
            seen.add(key);
            out.push(name);
          }
          return out;
        }
        """
    )
    return list(names or [])


async def click_calp_lv3(page: Page, name: str) -> bool:
    await dismiss_popups(page)
    return bool(
        await page.evaluate(
            """
            (targetName) => {
              const want = targetName.toLowerCase();
              const nodes = [...document.querySelectorAll(
                '[class*="lv3CategoryBox"], [class*="lv3Category"]'
              )];
              for (const el of nodes) {
                const text = (el.innerText || '').trim().replace(/\\s+/g, ' ');
                const label = text.split('\\n').map(s => s.trim()).filter(Boolean).pop() || text;
                if (label.toLowerCase() !== want && !label.toLowerCase().startsWith(want)) {
                  continue;
                }
                const clickable = el.closest('[class*="lv3CategoryBox"]') || el;
                clickable.scrollIntoView({ block: 'center' });
                clickable.click();
                return true;
              }
              return false;
            }
            """,
            name,
        )
    )


async def discover_direct_subcategories(
    page: Page,
    category_name: str,
    category_url: str,
    *,
    exclude_ids: set[str] | None = None,
) -> list[tuple[str, str]]:
    if is_calp_url(category_url):
        # Calp lv3 icons have no href; handled inside crawl_category via clicks.
        return []

    parent_id = category_id_from_url(category_url)
    site_host = site_host_from_url(category_url)
    excluded = exclude_ids or set()
    await safe_goto(page, category_url)
    await dismiss_popups(page)
    await handle_captcha(page)
    await scroll_listing_page(page)
    await sleep()

    raw_items = await page.evaluate(
        """
        ({ parentId, siteHost }) => {
          const results = [];
          const seen = new Set();
          const selectors = [
            'div[class*="refine"] a[href*="/category/"]',
            'div[class*="category"] a[href*="/category/"]',
            'aside a[href*="/category/"]',
            'nav a[href*="/category/"]',
            '[class*="sub-cate"] a[href*="/category/"]',
            'a[href*="/category/"]',
          ];
          const skipNames = new Set([
            'home',
            'all categories',
            'see all',
            'view all',
            'more',
          ]);
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
              if (skipNames.has(lowered)) continue;
              seen.add(catId);
              results.push({
                name,
                url: href.split('?')[0] + '?SortType=total_tranpro_desc',
              });
            }
          }
          return results;
        }
        """,
        {"parentId": parent_id, "siteHost": site_host},
    )

    subcategories: list[tuple[str, str]] = []
    for item in raw_items or []:
        cat_id = category_id_from_url(item["url"])
        if cat_id in excluded:
            continue
        subcategories.append((f"{category_name} > {item['name']}", item["url"]))
    return subcategories


async def discover_all_subcategories(
    page: Page,
    category_name: str,
    category_url: str,
    *,
    max_depth: int = MAX_SUBCATEGORY_DEPTH,
) -> list[tuple[str, str]]:
    root_id = category_id_from_url(category_url)
    seen_ids: set[str] = {root_id} if root_id else set()
    discovered: list[tuple[str, str]] = []
    frontier: list[tuple[str, str, int]] = [(category_name, category_url, 0)]

    while frontier:
        parent_name, parent_url, depth = frontier.pop(0)
        if depth >= max_depth:
            continue

        direct_subs = await discover_direct_subcategories(
            page,
            parent_name,
            parent_url,
            exclude_ids=seen_ids,
        )
        for sub_name, sub_url in direct_subs:
            sub_id = category_id_from_url(sub_url)
            if sub_id and sub_id in seen_ids:
                continue
            if sub_id:
                seen_ids.add(sub_id)
            discovered.append((sub_name, sub_url))
            frontier.append((sub_name, sub_url, depth + 1))

    return discovered


def save_new_products(
    products: list[ListingProduct],
    seen_keys: set[str],
    category_name: str,
    es_writer: ElasticsearchUrlWriter | None = None,
) -> int:
    """Append new URLs locally; always upsert ES so recrawls overwrite metrics."""
    new_count = 0
    LINKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LINKS_FILE.open("a", encoding="utf-8") as links_fh, PRODUCTS_JSONL.open(
        "a", encoding="utf-8"
    ) as jsonl_fh:
        for product in products:
            if es_writer is not None:
                es_writer.write(product, category_name)

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
            new_count += 1
    return new_count


async def crawl_infinite_scroll(
    page: Page,
    category_name: str,
    site_base: str,
    seen_links: set[str],
    collector: ListingCollector,
    es_writer: ElasticsearchUrlWriter | None = None,
) -> int:
    """Scroll until consecutive rounds yield no new product IDs."""
    total_new = 0
    seen_ids: set[str] = set()
    enriched_ids: set[str] = set()
    metrics_by_id: dict[str, ListingProduct] = {}
    consecutive_dup = 0
    round_no = 0

    while True:
        if MAX_SCROLL_ROUNDS > 0 and round_no >= MAX_SCROLL_ROUNDS:
            print(f"达到最大滚动轮数 {MAX_SCROLL_ROUNDS}，停止：{category_name}")
            break

        round_no += 1
        if round_no > 1:
            await scroll_listing_page(page)
        else:
            await dismiss_popups(page)
            if FIRST_ROUND_SETTLE_MS > 0:
                await page.wait_for_timeout(FIRST_ROUND_SETTLE_MS)

        await handle_captcha(page)
        products = await extract_products(page, site_base, collector)

        # Re-apply metrics gathered earlier this category run (esp. feedback enrich).
        restored: list[ListingProduct] = []
        for product in products:
            cached = metrics_by_id.get(product.product_id)
            if cached is None:
                restored.append(product)
                continue
            restored.append(
                ListingProduct(
                    product_id=product.product_id,
                    url=product.url or cached.url,
                    source=product.source or cached.source,
                    title=product.title or cached.title,
                    price=product.price if product.price is not None else cached.price,
                    rating=product.rating if product.rating is not None else cached.rating,
                    reviews=product.reviews if product.reviews is not None else cached.reviews,
                    sold_count=(
                        product.sold_count
                        if product.sold_count is not None
                        else cached.sold_count
                    ),
                )
            )
        products = restored

        current_ids = {p.product_id for p in products}
        fresh = current_ids - seen_ids

        if ENRICH_MODE == "all":
            await enrich_rating_reviews(page, products)
        elif ENRICH_MODE == "new":
            pending = [
                p
                for p in products
                if p.product_id not in enriched_ids
                and (p.rating is None or p.reviews is None)
            ]
            if pending:
                await enrich_rating_reviews(page, pending)
                enriched_ids.update(p.product_id for p in pending)

        for product in products:
            metrics_by_id[product.product_id] = product

        new_count = save_new_products(products, seen_links, category_name, es_writer)
        if es_writer is not None:
            es_writer.flush()
        total_new += new_count
        with_metrics = sum(
            1 for p in products if p.rating is not None and p.reviews is not None
        )
        print(
            f"滚动第 {round_no} 轮：可见/API 合计 {len(products)} 个"
            f"（{with_metrics} 个含评分+评论数），"
            f"本轮新 ID {len(fresh)} 个，保存新增 {new_count} 个"
        )

        if (not current_ids or not fresh) and round_no > 1:
            consecutive_dup += 1
        else:
            consecutive_dup = 0

        seen_ids.update(current_ids)

        if consecutive_dup >= CONSECUTIVE_DUPLICATE_SCROLLS_TO_STOP:
            print(
                f"连续 {consecutive_dup} 轮无新商品，判定 {category_name} 已到底。"
            )
            break

        await sleep(short=True)

    return total_new


async def _prepare_calp_lv3(page: Page, category_url: str, sub_name: str) -> bool:
    """点击 calp 子类目；失败时整页重开再点。"""
    await dismiss_popups(page)
    try:
        await page.evaluate("window.scrollTo(0, 0)")
    except Exception:
        pass
    await page.wait_for_timeout(300)
    if await click_calp_lv3(page, sub_name):
        if CALP_CLICK_SETTLE_MS > 0:
            await page.wait_for_timeout(CALP_CLICK_SETTLE_MS)
        return True
    await safe_goto(page, category_url)
    await dismiss_popups(page)
    await handle_captcha(page)
    await page.wait_for_timeout(500)
    if not await click_calp_lv3(page, sub_name):
        return False
    if CALP_CLICK_SETTLE_MS > 0:
        await page.wait_for_timeout(CALP_CLICK_SETTLE_MS)
    return True


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
    print(f"\n类目：{category_name}")
    print(f"打开：{category_url}")

    collector.clear()
    await safe_goto(page, category_url)
    await dismiss_popups(page)
    await handle_captcha(page)
    await sleep()

    total_new += await crawl_infinite_scroll(
        page, category_name, site_base, seen_links, collector, es_writer
    )

    if is_calp_url(category_url) and CRAWL_SUBCATEGORIES and " > " not in category_name:
        try:
            await page.evaluate("window.scrollTo(0, 0)")
        except Exception:
            pass
        await page.wait_for_timeout(500)
        lv3_names = await list_calp_lv3_names(page)
        if not lv3_names:
            await safe_goto(page, category_url)
            await dismiss_popups(page)
            await handle_captcha(page)
            await page.wait_for_timeout(800)
            lv3_names = await list_calp_lv3_names(page)
        if lv3_names:
            print(f"发现 {len(lv3_names)} 个 calp 子类目图标：{category_name}")
        for sub_name in lv3_names:
            target_name = f"{category_name} > {sub_name}"
            print(f"\n子类目（点击）：{target_name}")
            if CALP_RELOAD_EACH_LV3:
                await safe_goto(page, category_url)
                await dismiss_popups(page)
                await handle_captcha(page)
                await page.wait_for_timeout(500)
                if not await click_calp_lv3(page, sub_name):
                    print(f"未能点击子类目：{sub_name}")
                    continue
                if CALP_CLICK_SETTLE_MS > 0:
                    await page.wait_for_timeout(CALP_CLICK_SETTLE_MS)
            elif not await _prepare_calp_lv3(page, category_url, sub_name):
                print(f"未能点击子类目：{sub_name}")
                continue
            collector.clear()
            total_new += await crawl_infinite_scroll(
                page, target_name, site_base, seen_links, collector, es_writer
            )

    elif (not is_calp_url(category_url)) and " > " not in category_name:
        wholesale = wholesale_search_url(category_name, site_base)
        print(f"\n附加批发搜索：{wholesale}")
        collector.clear()
        await safe_goto(page, wholesale)
        await dismiss_popups(page)
        await handle_captcha(page)
        total_new += await crawl_infinite_scroll(
            page, category_name, site_base, seen_links, collector, es_writer
        )

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
    print(f"类目数量: {len(categories)}（US）")
    print(f"子类目发现: {'开启' if CRAWL_SUBCATEGORIES else '关闭'}")
    if CRAWL_SUBCATEGORIES:
        print(f"子类目最大深度: {MAX_SUBCATEGORY_DEPTH}")
        print(f"calp 每子类重开: {'开启' if CALP_RELOAD_EACH_LV3 else '关闭（页内点击）'}")
    print(
        f"滚动重复停止阈值: 连续 {CONSECUTIVE_DUPLICATE_SCROLLS_TO_STOP} 轮无新商品"
    )
    if MAX_SCROLL_ROUNDS > 0:
        print(f"最大滚动轮数: {MAX_SCROLL_ROUNDS}")
    print(f"请求间隔: {REQUEST_DELAY_MS[0]}–{REQUEST_DELAY_MS[1]} ms")
    print(f"评分补全: {ENRICH_MODE}" + (
        f"（并发 {ENRICH_CONCURRENCY}）" if ENRICH_MODE != "off" else ""
    ))
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
                # Classic /category/ seeds still auto-discover linked subcats.
                # Calp-plus lv3 icons are clicked inside crawl_category.
                if (
                    CRAWL_SUBCATEGORIES
                    and not is_calp_url(category_url)
                    and category_path_depth(category_name) < 3
                ):
                    for attempt in range(2):
                        try:
                            if context is None or page is None or page.is_closed():
                                await close_browser(context)
                                context, page, collector = await create_browser(playwright)
                            subs = await discover_all_subcategories(
                                page, category_name, category_url
                            )
                            if subs:
                                print(
                                    f"发现 {len(subs)} 个子类目（含多级）：{category_name}"
                                )
                                targets.extend(subs)
                            break
                        except Exception as exc:
                            if is_browser_dead(exc, page=page) and attempt == 0:
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
                            if is_browser_dead(exc, page=page) and attempt == 0:
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
