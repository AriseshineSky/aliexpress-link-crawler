# -*- coding: utf-8 -*-
"""
AliExpress 商品链接抓取（alilj.py）。

只抓列表页，不抓详情；从列表页 API + DOM 提取价格、评分、评论数、销量。
首页分类入口为 /p/calp-plus/?categoryTab=...，商品靠无限下滑加载。
批发 /w/wholesale-* 与 /category/* 多为翻页；运行时按页面 DOM 自动选择下滑或翻页。
默认附加 maxPrice / 4StarRating / SortType=total_tranpro_desc（销量优先）。
页面无 reviews 数量排序；本地 QUALITY_* 仅统计质量通过数，抓到的 id 一律写入 ES/本地。
calp 子类目：发现后写入 ES 为批发搜索 URL（可认领），并循环抓取各 URL。
类目 URL 预发现：discover_categories.py / --discover-categories（先爬 L1+lv3，再抓商品）。
默认只抓 aliexpress.us；支持滑块验证码自动拖动 + 手动兜底。
多 worker 并行：分类入队，CRAWL_WORKERS 个独立浏览器各自领取类目。
多设备：ES 类目索引认领（claimed + lease），抓完标记 done 并写产品数。
默认 CRAWL_RECLAIM_DONE=1：已 done 的 URL 仍可再认领，循环抓取；每次随机选可抓 seed。
CRAWL_LOOP=1 时整轮结束后重置 pending 并重复；轮次时间写入 __crawl_round__。
输出：产品链接.txt、产品列表.jsonl、Elasticsearch（ELASTICSEARCH_INDEX_URLS）
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import random
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

import pyautogui
import yaml
from dotenv import load_dotenv
from elasticsearch import Elasticsearch, helpers
from playwright.async_api import BrowserContext, Page, async_playwright

from category_claim import (
    CategoryClaimClient,
    CategoryCrawlStats,
    ClaimedCategory,
    default_device_id,
)

BASE_DIR = Path(__file__).resolve().parent
USER_DATA_DIR = BASE_DIR / "browser"
LINKS_FILE = BASE_DIR / "产品链接.txt"
PRODUCTS_JSONL = BASE_DIR / "产品列表.jsonl"
CATEGORIES_FILE = BASE_DIR / "config" / "categories.yaml"

HEADLESS = False
CAPTCHA_WAIT_SECONDS = 120
CRAWL_SUBCATEGORIES = True
MAX_SUBCATEGORY_DEPTH = 5
MAX_PAGES_PER_CATEGORY = 0  # 翻页模式：0=不限制；>0 为最大页数
MAX_SCROLL_ROUNDS = 0  # 下滑模式：0=不限制，直到连续无新商品
CONSECUTIVE_DUPLICATE_SCROLLS_TO_STOP = 10
CONSECUTIVE_DUPLICATE_PAGES_TO_STOP = 3  # 翻页：连续 N 页无新 ID 则停
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
CRAWL_WORKERS = 2  # 并行 worker 数；每个 worker 独立浏览器，从队列领取分类
LOGIN_RECOVERY_RETRIES = 8  # 登录/风控拦截后重新打开目标页的次数
# Multi-device ES category claim (requires ELASTICSEARCH_INDEX_CATEGORIES).
CATEGORY_CLAIM_MODE = True  # auto-disabled when ES categories unavailable
CLAIM_BATCH_SIZE = 0  # 0 = CRAWL_WORKERS（每批给本机所有 worker）
CLAIM_LEASE_SECONDS = 7200  # 占用租约；超时可被其他设备回收
CLAIM_HEARTBEAT_SECONDS = 900  # 抓取中续租间隔
DEVICE_ID = ""  # 空 = hostname；多机请设不同值
CRAWL_RECLAIM_DONE = True  # True = 已 done 仍可再认领（循环抓）；False = 抓过就跳过
# 打开分类/列表页时附加的 URL 过滤（与站内 selectedSwitches / minPrice / maxPrice 一致）
LISTING_MAX_PRICE = "99"  # 空字符串 = 不加价格上限
LISTING_MIN_PRICE = ""  # 空字符串 = 不加价格下限
LISTING_STAR_FILTER = "4StarRating"  # 空字符串 = 不加星级开关；例: 4StarRating
# SortType works on /w/wholesale-*.html and /category/*.html; also attached to calp URLs.
# AliExpress has NO listing SortType for review-count — use QUALITY_MIN_REVIEWS locally.
LISTING_SORT_TYPE = "total_tranpro_desc"  # orders/sold desc; empty = no SortType
# Local quality gate after enrich (page cannot filter reviews/sold). 0/empty = off.
QUALITY_FILTER = True
QUALITY_MAX_PRICE = 100.0
QUALITY_MIN_RATING = 4.4
QUALITY_MIN_REVIEWS = 300
QUALITY_MIN_SOLD = 500
# Discover calp lv3 → upsert wholesale URLs into ES as separate claimable seeds.
SUBCATEGORY_AS_SEEDS = True
# Continuous crawl: after a full pass, reset all seeds to pending and repeat.
CRAWL_LOOP = True
CRAWL_LOOP_SLEEP_SEC = 120
# Serialize local file + shared seen_links updates across workers.
FILE_IO_LOCK = threading.Lock()
# Current crawl round number (set in main_async).
CURRENT_CRAWL_ROUND = 0

LOGIN_URL_MARKERS = (
    "login.aliexpress.",
    "passport.aliexpress.",
    "sso.aliexpress.",
    "/account/login",
    "/login?",
    "/login/",
    "/login.html",
    "signin",
)
CATEGORY_BLACKLIST_FILE = BASE_DIR / "config" / "category_blacklist.yaml"
# Defaults; overridden by category_blacklist.yaml and/or env after load_dotenv.
BLOCKED_CATEGORY_TABS: frozenset[str] = frozenset(
    {
        "women's_clothing",
        "men's_clothing",
        "women_s_clothing",
        "men_s_clothing",
        "novelty_&_special_use",
        "novelty_%26_special_use",
    }
)
CATEGORY_BLACKLIST_KEYWORDS: tuple[str, ...] = (
    # clothing
    "women's clothing",
    "men's clothing",
    "apparel",
    "clothing",
    "clothes",
    "dress",
    "dresses",
    "shirt",
    "shirts",
    "blouse",
    "skirt",
    "skirts",
    "jeans",
    "hoodie",
    "sweatshirt",
    "sweater",
    "jacket",
    "jackets",
    "coat",
    "coats",
    "underwear",
    "lingerie",
    "swimsuit",
    "swimwear",
    "bikini",
    "t-shirt",
    "tshirt",
    "pants",
    "trousers",
    "shorts",
    "socks",
    "activewear",
    "sportswear",
    # adult / sensitive
    "novelty & special use",
    "novelty and special use",
    "adult novelty",
    "adult product",
    "adult products",
    "adult toy",
    "adult toys",
    "adult sex",
    "sex toy",
    "sex toys",
    "sex product",
    "sex products",
    "erotic",
    "erotica",
    "vibrator",
    "vibrators",
    "dildo",
    "dildos",
    "masturbator",
    "masturbation",
    "bdsm",
    "fetish",
    "bondage",
    "intimate toy",
    "intimate toys",
    "pleasure toy",
    "pleasure toys",
    "sexy lingerie",
    "adult entertainment",
    "成人",
    "情趣",
    "性用品",
    "成人用品",
)

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
if blacklist_file := (os.getenv("CATEGORY_BLACKLIST_FILE") or "").strip():
    CATEGORY_BLACKLIST_FILE = Path(blacklist_file)
    if not CATEGORY_BLACKLIST_FILE.is_absolute():
        CATEGORY_BLACKLIST_FILE = BASE_DIR / CATEGORY_BLACKLIST_FILE


def _parse_csv_lower(raw: str) -> list[str]:
    return [part.strip().lower() for part in raw.split(",") if part.strip()]


def _load_category_blacklist() -> None:
    """Merge YAML blacklist + env overrides into module-level frozensets/tuples."""
    global BLOCKED_CATEGORY_TABS, CATEGORY_BLACKLIST_KEYWORDS
    tabs = set(BLOCKED_CATEGORY_TABS)
    keywords = list(CATEGORY_BLACKLIST_KEYWORDS)
    path = CATEGORY_BLACKLIST_FILE
    if path.exists():
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            for tab in raw.get("tabs") or []:
                text = str(tab).strip().lower()
                if text:
                    tabs.add(text)
            file_keywords = [str(k).strip().lower() for k in (raw.get("keywords") or []) if str(k).strip()]
            if file_keywords:
                keywords = file_keywords
        except Exception as exc:
            print(f"读取类目黑名单失败 {path}: {exc}")
    if env_tabs := (os.getenv("CATEGORY_BLACKLIST_TABS") or "").strip():
        tabs.update(_parse_csv_lower(env_tabs))
    if env_keywords := (os.getenv("CATEGORY_BLACKLIST_KEYWORDS") or "").strip():
        keywords = _parse_csv_lower(env_keywords)
    BLOCKED_CATEGORY_TABS = frozenset(tabs)
    CATEGORY_BLACKLIST_KEYWORDS = tuple(dict.fromkeys(keywords))


_load_category_blacklist()
if max_pages := (os.getenv("MAX_PAGES_PER_CATEGORY") or "").strip():
    MAX_PAGES_PER_CATEGORY = int(max_pages)
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
if dup_pages := (os.getenv("CONSECUTIVE_DUPLICATE_PAGES_TO_STOP") or "").strip():
    CONSECUTIVE_DUPLICATE_PAGES_TO_STOP = max(1, int(dup_pages))
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
if (max_price := os.getenv("LISTING_MAX_PRICE")) is not None:
    LISTING_MAX_PRICE = max_price.strip()
if (min_price := os.getenv("LISTING_MIN_PRICE")) is not None:
    LISTING_MIN_PRICE = min_price.strip()
if (star_filter := os.getenv("LISTING_STAR_FILTER")) is not None:
    LISTING_STAR_FILTER = star_filter.strip()
if (sort_type := os.getenv("LISTING_SORT_TYPE")) is not None:
    LISTING_SORT_TYPE = sort_type.strip()
if (os.getenv("QUALITY_FILTER") or "").strip().lower() in {"0", "false", "no"}:
    QUALITY_FILTER = False
if (os.getenv("QUALITY_FILTER") or "").strip().lower() in {"1", "true", "yes"}:
    QUALITY_FILTER = True
if (q_max := os.getenv("QUALITY_MAX_PRICE")) is not None and q_max.strip():
    QUALITY_MAX_PRICE = float(q_max.strip())
if (q_rating := os.getenv("QUALITY_MIN_RATING")) is not None and q_rating.strip():
    QUALITY_MIN_RATING = float(q_rating.strip())
if (q_rev := os.getenv("QUALITY_MIN_REVIEWS")) is not None and q_rev.strip():
    QUALITY_MIN_REVIEWS = int(q_rev.strip())
if (q_sold := os.getenv("QUALITY_MIN_SOLD")) is not None and q_sold.strip():
    QUALITY_MIN_SOLD = int(q_sold.strip())
if (os.getenv("SUBCATEGORY_AS_SEEDS") or "").strip().lower() in {"0", "false", "no"}:
    SUBCATEGORY_AS_SEEDS = False
if (os.getenv("SUBCATEGORY_AS_SEEDS") or "").strip().lower() in {"1", "true", "yes"}:
    SUBCATEGORY_AS_SEEDS = True
if (os.getenv("CRAWL_LOOP") or "").strip().lower() in {"0", "false", "no"}:
    CRAWL_LOOP = False
if (os.getenv("CRAWL_LOOP") or "").strip().lower() in {"1", "true", "yes"}:
    CRAWL_LOOP = True
if loop_sleep := (os.getenv("CRAWL_LOOP_SLEEP_SEC") or "").strip():
    CRAWL_LOOP_SLEEP_SEC = max(0, int(loop_sleep))
if crawl_workers := (os.getenv("CRAWL_WORKERS") or "").strip():
    CRAWL_WORKERS = max(1, int(crawl_workers))
if login_retries := (os.getenv("LOGIN_RECOVERY_RETRIES") or "").strip():
    LOGIN_RECOVERY_RETRIES = max(1, int(login_retries))
if (os.getenv("CATEGORY_CLAIM_MODE") or "").strip().lower() in {"0", "false", "no"}:
    CATEGORY_CLAIM_MODE = False
if (os.getenv("CATEGORY_CLAIM_MODE") or "").strip().lower() in {"1", "true", "yes"}:
    CATEGORY_CLAIM_MODE = True
if claim_batch := (os.getenv("CLAIM_BATCH_SIZE") or "").strip():
    CLAIM_BATCH_SIZE = max(0, int(claim_batch))
if claim_lease := (os.getenv("CLAIM_LEASE_SECONDS") or "").strip():
    CLAIM_LEASE_SECONDS = max(60, int(claim_lease))
if claim_hb := (os.getenv("CLAIM_HEARTBEAT_SECONDS") or "").strip():
    CLAIM_HEARTBEAT_SECONDS = max(30, int(claim_hb))
if device_id_env := (os.getenv("DEVICE_ID") or "").strip():
    DEVICE_ID = device_id_env
if (os.getenv("CRAWL_RECLAIM_DONE") or "").strip().lower() in {"0", "false", "no"}:
    CRAWL_RECLAIM_DONE = False
if (os.getenv("CRAWL_RECLAIM_DONE") or "").strip().lower() in {"1", "true", "yes"}:
    CRAWL_RECLAIM_DONE = True

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = window.chrome || { runtime: {} };
"""
CHROMIUM_ARGS_BASE = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
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


def worker_log(worker_id: int, message: str) -> None:
    print(f"[W{worker_id}] {message}")


def site_base_from_url(url: str) -> str:
    host = urlsplit(url).netloc.lower()
    if host.endswith("aliexpress.com"):
        return "https://www.aliexpress.com"
    return "https://www.aliexpress.us"


def source_from_url(url: str) -> str:
    return "aliexpress.com" if site_base_from_url(url) == "https://www.aliexpress.com" else "aliexpress.us"


def site_host_from_url(url: str) -> str:
    return urlsplit(site_base_from_url(url)).netloc


def category_tab_of(url: str) -> str:
    query = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))
    return unquote(query.get("categoryTab") or "").strip().lower()


def _text_hits_blacklist(text: str) -> str | None:
    """Return the first matching blacklist keyword, else None."""
    lowered = (text or "").strip().lower()
    if not lowered:
        return None
    # Normalize categoryTab-style tokens for keyword checks.
    normalized = (
        lowered.replace("_", " ").replace("%27", "'").replace("'", "'")
    )
    for keyword in CATEGORY_BLACKLIST_KEYWORDS:
        if not keyword:
            continue
        # Word/phrase boundary so "coat" does not match "coating".
        pattern = rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])"
        if re.search(pattern, normalized):
            return keyword
    return None


def is_blacklisted_category(*, name: str = "", url: str = "") -> bool:
    """True if seed name, lv3 label, or calp categoryTab is on the clothing blacklist."""
    tab = category_tab_of(url) if url else ""
    if tab and tab in BLOCKED_CATEGORY_TABS:
        return True
    if tab and _text_hits_blacklist(tab):
        return True
    if name and _text_hits_blacklist(name):
        return True
    return False


def is_blocked_category_url(url: str) -> bool:
    return is_blacklisted_category(url=url)


def filter_allowed_categories(
    rows: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    allowed: list[tuple[str, str]] = []
    for name, url in rows:
        if is_blacklisted_category(name=name, url=url):
            print(f"跳过黑名单类目：{name}")
            continue
        allowed.append((name, url))
    return allowed


def load_categories() -> list[tuple[str, str]]:
    """Load (name, calp_url) from ES crawl index, else categories.yaml."""
    es_index = (os.getenv("ELASTICSEARCH_INDEX_CATEGORIES") or "").strip()
    es_url = (os.getenv("ELASTICSEARCH_URL") or "").strip()
    rows: list[tuple[str, str]] = []
    if es_index and es_url:
        try:
            rows = _load_categories_from_es(es_url, es_index)
            if rows:
                print(f"Loaded {len(rows)} categories from ES index={es_index}")
            else:
                print(f"ES index={es_index} empty; falling back to {CATEGORIES_FILE}")
        except Exception as exc:
            print(f"ES category load failed ({exc}); falling back to {CATEGORIES_FILE}")
            rows = []

    if not rows:
        with CATEGORIES_FILE.open(encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
        rows = [(item["name"], item["url"]) for item in raw["categories"]]
        print(f"Loaded {len(rows)} categories from {CATEGORIES_FILE}")

    return filter_allowed_categories(rows)


def _load_categories_from_es(es_url: str, index: str) -> list[tuple[str, str]]:
    client = Elasticsearch(hosts=[es_url], request_timeout=60)
    enabled_only = (os.getenv("CATEGORIES_ENABLED_ONLY") or "1").strip().lower() not in {
        "0",
        "false",
        "no",
    }
    must: list[dict] = []
    if enabled_only:
        must.append({"term": {"enabled": True}})
    query: dict = {"match_all": {}} if not must else {"bool": {"must": must}}
    resp = client.search(
        index=index,
        size=500,
        query=query,
        sort=[{"priority": {"order": "asc", "unmapped_type": "long"}}],
    )
    rows: list[tuple[str, str]] = []
    for hit in resp.get("hits", {}).get("hits", []):
        src = hit.get("_source") or {}
        name = str(src.get("name") or "").strip()
        url = str(src.get("url") or "").strip()
        if name and url:
            rows.append((name, url))
    return rows


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
        self.listing_total: int | None = None

    def clear(self) -> None:
        self.search_payloads.clear()
        self.listing_total = None

    def note_listing_total(self, total: int | None) -> None:
        if total is None or total <= 0:
            return
        if self.listing_total is None or total > self.listing_total:
            self.listing_total = total

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
            self.note_listing_total(_extract_listing_total_from_payload(payload))
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


def _extract_listing_total_from_payload(payload: dict) -> int | None:
    """Best-effort total result count from list/recommend API JSON."""
    found: list[int] = []

    def walk(node, depth: int = 0) -> None:
        if depth > 10 or len(found) >= 8:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                key_l = str(key).lower()
                if key_l in {
                    "totalcount",
                    "total",
                    "totalsearchcount",
                    "totalresults",
                    "resultcount",
                    "itemcount",
                    "productcount",
                    "productscount",
                    "numfound",
                    "totalitems",
                    "totalnum",
                    "totalhits",
                    "estimatedtotal",
                }:
                    parsed = _digits(value) if not isinstance(value, bool) else None
                    if parsed is not None and parsed > 0:
                        found.append(parsed)
                if isinstance(value, (dict, list)):
                    walk(value, depth + 1)
        elif isinstance(node, list):
            for item in node[:30]:
                walk(item, depth + 1)

    walk(payload)
    return max(found) if found else None


async def extract_listing_total_from_dom(page: Page) -> int | None:
    """Read result-count text from the listing page when present."""
    try:
        raw = await page.evaluate(
            """
            () => {
              const body = (document.body && document.body.innerText) || '';
              const patterns = [
                /([\\d,]+)\\+?\\s*results?/i,
                /([\\d,]+)\\+?\\s*items?/i,
                /([\\d,]+)\\+?\\s*products?/i,
                /over\\s+([\\d,]+)/i,
              ];
              for (const re of patterns) {
                const m = body.match(re);
                if (m) return m[1];
              }
              const nodes = document.querySelectorAll(
                '[class*="result"], [class*="Result"], [class*="total"], [class*="Total"]'
              );
              for (const el of nodes) {
                const t = (el.innerText || '').trim();
                if (t.length > 40) continue;
                const m = t.match(/([\\d,]+)\\+?/);
                if (m && /result|item|product/i.test(t)) return m[1];
              }
              return null;
            }
            """
        )
    except Exception:
        return None
    return _digits(raw)


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


def _product_es_fingerprint(product: ListingProduct, category_name: str) -> tuple:
    """Compare payload meaningfully — skip ES re-upsert when unchanged."""
    return (
        product.dedupe_key,
        category_name,
        product.url,
        product.title,
        product.price,
        product.rating,
        product.reviews,
        product.sold_count,
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
        self.skipped = 0
        self._lock = threading.Lock()
        # Per-run cache: same doc fingerprint → do not bulk again.
        self._written_fp: dict[str, tuple] = {}

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

        fingerprint = _product_es_fingerprint(product, category_name)
        with self._lock:
            if self._written_fp.get(product.dedupe_key) == fingerprint:
                self.skipped += 1
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
            fields["quality_passed"] = product_passes_quality(product)

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
            self._written_fp[product.dedupe_key] = fingerprint
            if len(self.buffer) >= self.chunk_size:
                self._flush_unlocked()

    def flush(self) -> None:
        with self._lock:
            self._flush_unlocked()

    def _flush_unlocked(self) -> None:
        if not self.enabled or self.client is None or not self.buffer:
            return

        batch = self.buffer
        self.buffer = []
        try:
            success_count, errors = helpers.bulk(
                self.client,
                batch,
                raise_on_error=False,
            )
        except Exception as exc:
            self.failed += len(batch)
            print(f"ES bulk 失败（已跳过 {len(batch)} 条）：{exc}")
            return
        failed_count = len(errors) if isinstance(errors, list) else 0
        self.saved += int(success_count)
        self.failed += failed_count
        if failed_count:
            print(f"ES bulk 失败 {failed_count} 条，首条错误: {errors[0]}")
        else:
            print(f"已写入 ES {int(success_count)} 条 -> {self.index}")

    def close(self) -> None:
        self.flush()
        with self._lock:
            if self.client is not None:
                self.client.close()
                self.client = None


def with_listing_filters(url: str) -> str:
    """Attach price / star / SortType filters used by AliExpress listing pages.

    Example:
      ...&selectedSwitches=filterCode%3A4StarRating&maxPrice=99&SortType=total_tranpro_desc

    Notes:
    - SortType=total_tranpro_desc sorts by orders/sold (works on wholesale & category).
    - Calp-plus may ignore SortType in the SPA; we still attach it and also try UI click.
    - There is no official SortType for review-count; filter reviews locally.
    """
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    if LISTING_MIN_PRICE:
        query["minPrice"] = LISTING_MIN_PRICE
    if LISTING_MAX_PRICE:
        query["maxPrice"] = LISTING_MAX_PRICE
    if LISTING_STAR_FILTER:
        code = LISTING_STAR_FILTER
        if not code.startswith("filterCode:"):
            code = f"filterCode:{code}"
        query["selectedSwitches"] = code
    if LISTING_SORT_TYPE:
        # Prefer capital SortType (AE wholesale); also set sortType for some pages.
        query["SortType"] = LISTING_SORT_TYPE
        query.setdefault("sortType", LISTING_SORT_TYPE)
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


def wholesale_slug(name: str) -> str:
    clean = name.split(" / ", 1)[-1].split(" > ", 1)[-1].strip()
    return quote(
        clean.lower().replace("&", "and").replace(",", "").replace(" ", "-"),
        safe="-",
    )


def wholesale_search_url(category_name: str, site_base: str) -> str:
    slug = wholesale_slug(category_name)
    return with_listing_filters(f"{site_base}/w/wholesale-{slug}.html")


def build_page_url(url: str, page_no: int) -> str:
    """Attach ?page=N for wholesale / classic category pagination."""
    if page_no <= 1:
        return url
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["page"] = str(page_no)
    match = re.search(r"/category/(\d+)/", parts.path)
    if match and "CatId" not in query:
        query["CatId"] = match.group(1)
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


def infer_listing_load_mode(
    *,
    has_pagination_dom: bool,
    url: str = "",
) -> str:
    """Decide scroll vs pagination from DOM signal + URL shape.

    Returns \"pagination\" or \"scroll\".
    """
    if has_pagination_dom:
        return "pagination"
    text = (url or "").lower()
    if "/p/calp-plus/" in text or "categorytab=" in text:
        return "scroll"
    if "/w/wholesale-" in text or re.search(r"/category/\d+/", text):
        return "pagination"
    return "scroll"


def calp_lv3_query_name(url: str) -> str:
    query = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))
    return unquote(query.get("calpLv3") or "").strip()


def with_calp_lv3(url: str, lv3_name: str) -> str:
    """Durable calp seed URL that records which in-page lv3 chip to click."""
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["calpLv3"] = lv3_name
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


def product_passes_quality(product: ListingProduct) -> bool:
    """Local quality gate (reviews/sold/rating/price). Page has no reviews SortType."""
    if not QUALITY_FILTER:
        return True
    if product.price is None or product.price >= QUALITY_MAX_PRICE:
        return False
    if product.rating is None or product.rating < QUALITY_MIN_RATING:
        return False
    if product.reviews is None or product.reviews < QUALITY_MIN_REVIEWS:
        return False
    if product.sold_count is None or product.sold_count < QUALITY_MIN_SOLD:
        return False
    return True


async def apply_orders_sort_ui(page: Page, *, worker_id: int = 0) -> bool:
    """Click the Orders / Most orders sort control when present (calp or search)."""
    try:
        clicked = await page.evaluate(
            """
            () => {
              const want = /^(orders|most\\s*orders|top\\s*sales)$/i;
              const nodes = [...document.querySelectorAll(
                'a, button, span, div, li, [role="tab"], [role="button"]'
              )];
              for (const el of nodes) {
                const t = (el.innerText || el.textContent || '').trim();
                if (!t || t.length > 24 || t.includes('\\n')) continue;
                if (!want.test(t)) continue;
                const clickable = el.closest('a, button, [role="tab"], [role="button"]') || el;
                clickable.scrollIntoView({ block: 'center' });
                clickable.click();
                return true;
              }
              return false;
            }
            """
        )
        if clicked:
            worker_log(worker_id, "已点击页面 Orders 排序")
            await page.wait_for_timeout(1200)
            return True
    except Exception as exc:
        if is_browser_dead(exc, page=page):
            raise
    return False


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


def is_login_url(url: str) -> bool:
    lowered = (url or "").lower()
    return any(marker in lowered for marker in LOGIN_URL_MARKERS)


def detect_screen_size() -> tuple[int, int]:
    """Best-effort screen size for tiling headed browsers."""
    if w := (os.getenv("SCREEN_WIDTH") or "").strip():
        if h := (os.getenv("SCREEN_HEIGHT") or "").strip():
            return max(800, int(w)), max(600, int(h))
    for cmd in (
        ["xdotool", "getdisplaygeometry"],
        ["xdpyinfo"],
    ):
        binary = cmd[0]
        if not shutil.which(binary):
            continue
        try:
            out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
        except Exception:
            continue
        if binary == "xdotool":
            parts = out.strip().split()
            if len(parts) >= 2:
                return max(800, int(parts[0])), max(600, int(parts[1]))
        match = re.search(r"dimensions:\s*(\d+)x(\d+)", out)
        if match:
            return max(800, int(match.group(1))), max(600, int(match.group(2)))
    try:
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        size = (max(800, int(root.winfo_screenwidth())), max(600, int(root.winfo_screenheight())))
        root.destroy()
        return size
    except Exception:
        return 1920, 1080


def worker_window_bounds(worker_id: int, workers: int) -> tuple[int, int, int, int]:
    """Tile workers left-to-right, top-to-bottom. Returns x, y, width, height."""
    workers = max(1, workers)
    worker_id = max(0, min(worker_id, workers - 1))
    screen_w, screen_h = detect_screen_size()
    cols = math.ceil(math.sqrt(workers))
    rows = math.ceil(workers / cols)
    width = max(640, screen_w // cols)
    height = max(480, screen_h // rows)
    col = worker_id % cols
    row = worker_id // cols
    return col * width, row * height, width, height


async def apply_window_bounds(page: Page, x: int, y: int, width: int, height: int) -> None:
    """Force Chromium window placement after launch (more reliable than CLI alone)."""
    if page.is_closed() or HEADLESS:
        return
    try:
        session = await page.context.new_cdp_session(page)
        meta = await session.send("Browser.getWindowForTarget")
        window_id = meta.get("windowId")
        if window_id is None:
            return
        await session.send(
            "Browser.setWindowBounds",
            {
                "windowId": window_id,
                "bounds": {
                    "left": int(x),
                    "top": int(y),
                    "width": int(width),
                    "height": int(height),
                    "windowState": "normal",
                },
            },
        )
    except Exception as exc:
        print(f"平铺窗口失败（可忽略）: {exc}")


async def is_login_page(page: Page) -> bool:
    if page.is_closed():
        return False
    return is_login_url(page.url or "")


async def sniff_captcha_signals(page: Page) -> bool:
    """轻量风控检测：title / URL / 正文前 2k（不含登录页）。"""
    if page.is_closed():
        return False
    if await is_login_page(page):
        return False
    url = (page.url or "").lower()
    if any(marker in url for marker in ("punish", "captcha", "_____tmd_____", "security")):
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


async def wait_until_unblocked(page: Page, *, kind: str, worker_id: int = 0) -> bool:
    """Wait for manual login/captcha clearance. Returns True if page looks clear."""
    worker_log(
        worker_id,
        f"检测到{kind}页面：{page.url}；请在该窗口完成操作，最多等待 {CAPTCHA_WAIT_SECONDS} 秒。",
    )
    waited = 0
    while waited < CAPTCHA_WAIT_SECONDS:
        if page.is_closed():
            raise RuntimeError("浏览器页面已关闭，请重启爬虫")
        if kind != "登录":
            await drag_slider_if_present(page)
        try:
            await page.wait_for_timeout(2000)
        except Exception as exc:
            if is_browser_dead(exc, page=page):
                raise
            continue
        waited += 2
        still_login = await is_login_page(page)
        still_captcha = await sniff_captcha_signals(page)
        if kind == "登录" and not still_login:
            worker_log(worker_id, "登录已完成，准备重新打开目标页。")
            return True
        if kind != "登录" and not still_login and not still_captcha:
            worker_log(worker_id, "验证已通过，准备重新打开目标页。")
            return True
    worker_log(worker_id, f"等待{kind}结束，可能仍未恢复；将重试目标页。")
    return False


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


async def _goto_network(page: Page, url: str, *, worker_id: int = 0) -> None:
    """Single navigation with transient network retries only."""
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
                worker_log(
                    worker_id,
                    f"网络异常 ({attempt}/{GOTO_MAX_RETRIES})：{exc}；"
                    f"{wait_s:.1f}s 后重试：{url}",
                )
                await asyncio.sleep(wait_s)
                continue
            raise


async def ensure_seed_category_tab(
    page: Page, seed_url: str, *, worker_id: int = 0
) -> None:
    """If top-nav click drifts to another L1 tab (e.g. women's_clothing), pull back."""
    if page.is_closed() or not is_calp_url(seed_url):
        return
    expected = category_tab_of(seed_url)
    current = category_tab_of(page.url or "")
    if not expected:
        return
    if current == expected and not is_blocked_category_url(page.url or ""):
        return
    worker_log(
        worker_id,
        f"类目 tab 跑偏（当前={current or '?'}，期望={expected}），重新打开种子页",
    )
    await safe_goto(page, seed_url, worker_id=worker_id)


async def safe_goto(page: Page, url: str, *, worker_id: int = 0) -> None:
    """Goto url; if redirected to login/captcha, wait then reopen the target."""
    if page.is_closed():
        raise RuntimeError("浏览器页面已关闭，请重启爬虫")
    if is_blocked_category_url(url):
        worker_log(worker_id, f"拒绝打开禁止类目 URL：{url}")
        raise RuntimeError(f"blocked category URL: {url}")

    for recovery in range(1, LOGIN_RECOVERY_RETRIES + 1):
        await _goto_network(page, url, worker_id=worker_id)
        await dismiss_popups(page)

        if await is_login_page(page):
            worker_log(
                worker_id,
                f"被重定向到登录页 ({recovery}/{LOGIN_RECOVERY_RETRIES})",
            )
            await wait_until_unblocked(page, kind="登录", worker_id=worker_id)
            worker_log(worker_id, f"重新打开目标页：{url}")
            continue

        slider_dragged = await drag_slider_if_present(page)
        if slider_dragged or await sniff_captcha_signals(page):
            worker_log(
                worker_id,
                f"触发验证/风控 ({recovery}/{LOGIN_RECOVERY_RETRIES})",
            )
            await wait_until_unblocked(page, kind="验证/风控", worker_id=worker_id)
            worker_log(worker_id, f"重新打开目标页：{url}")
            continue

        # Still somehow on a login URL after overlays — retry.
        if is_login_url(page.url or ""):
            worker_log(worker_id, f"仍停留在登录相关页，重试：{page.url}")
            continue
        return

    await _goto_network(page, url, worker_id=worker_id)
    await dismiss_popups(page)
    if await is_login_page(page) or await sniff_captcha_signals(page):
        worker_log(
            worker_id,
            f"警告：多次重试后仍可能在登录/风控页：{page.url}",
        )


async def handle_captcha(page: Page, *, worker_id: int = 0) -> bool:
    """Compatibility wrapper: wait out captcha/login then return."""
    if page.is_closed():
        raise RuntimeError("浏览器页面已关闭，请重启爬虫")
    await dismiss_popups(page)

    if await is_login_page(page):
        return await wait_until_unblocked(page, kind="登录", worker_id=worker_id)

    slider_dragged = await drag_slider_if_present(page)
    if not slider_dragged and not await sniff_captcha_signals(page):
        return False
    return await wait_until_unblocked(page, kind="验证/风控", worker_id=worker_id)


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


async def _scroll_by_smooth(page: Page, delta_y: int, *, steps: int | None = None) -> None:
    """Ease-in-out scroll curve (more human than a single scrollBy jump)."""
    if page.is_closed() or delta_y == 0:
        return
    distance = abs(int(delta_y))
    direction = 1 if delta_y > 0 else -1
    step_n = steps if steps is not None else max(6, min(18, distance // 80))
    # Ease-in-out weights so early/late steps are smaller.
    weights = []
    for i in range(step_n):
        t = (i + 0.5) / step_n
        # Smoothstep curve.
        ease = t * t * (3 - 2 * t)
        prev = ((i) / step_n)
        prev_ease = prev * prev * (3 - 2 * prev)
        weights.append(max(0.01, ease - prev_ease))
    total_w = sum(weights) or 1.0
    moved = 0
    for i, w in enumerate(weights):
        chunk = int(round(distance * (w / total_w)))
        if i == step_n - 1:
            chunk = distance - moved
        if chunk <= 0:
            continue
        moved += chunk
        try:
            await page.evaluate("(dy) => window.scrollBy(0, dy)", chunk * direction)
        except Exception as exc:
            if is_browser_dead(exc, page=page):
                raise
            return
        await page.wait_for_timeout(random.randint(18, 55))


async def _page_scroll_metrics(page: Page) -> tuple[float, float, float]:
    """Return scrollY, viewportHeight, scrollHeight."""
    data = await page.evaluate(
        """() => ({
          y: window.scrollY || document.documentElement.scrollTop || 0,
          vh: window.innerHeight || 800,
          sh: Math.max(
            document.body ? document.body.scrollHeight : 0,
            document.documentElement ? document.documentElement.scrollHeight : 0
          ),
        })"""
    )
    return float(data["y"]), float(data["vh"]), float(data["sh"])


async def scroll_listing_page(page: Page) -> None:
    """Human-like scroll: curved downs, occasional ups, bounce near bottom."""
    await dismiss_popups(page)
    try:
        y, vh, sh = await _page_scroll_metrics(page)
    except Exception as exc:
        if is_browser_dead(exc, page=page):
            raise
        y, vh, sh = 0.0, 800.0, 3000.0

    remaining = max(0.0, sh - (y + vh))
    near_bottom = remaining < max(240.0, vh * 0.35)

    async def do_gesture() -> None:
        if near_bottom:
            # Bounce: scroll up a bit, pause, then continue down past previous bottom.
            up = -int(vh * random.uniform(0.25, 0.65) + random.randint(80, 220))
            await _scroll_by_smooth(page, up)
            await page.wait_for_timeout(random.randint(280, 700))
            down = int(vh * random.uniform(0.85, 1.45) + random.randint(200, 500))
            await _scroll_by_smooth(page, down)
            return

        # Main downward read, sometimes with a small digression upward.
        if random.random() < 0.28:
            nudge_up = -int(vh * random.uniform(0.08, 0.28) + random.randint(40, 120))
            await _scroll_by_smooth(page, nudge_up, steps=random.randint(4, 8))
            await page.wait_for_timeout(random.randint(120, 380))

        down = int(vh * random.uniform(0.55, 1.05) + random.randint(180, 520))
        # Avoid always jumping a full viewport; vary pace.
        await _scroll_by_smooth(page, down)

        if random.random() < 0.18:
            await page.wait_for_timeout(random.randint(150, 420))
            micro = int(random.choice([-1, 1]) * random.randint(60, 180))
            await _scroll_by_smooth(page, micro, steps=random.randint(3, 6))

    # Human gestures take longer than a single jump; give the list API more time.
    api_timeout = SCROLL_API_TIMEOUT_MS + (5000 if near_bottom else 2500)
    try:
        async with page.expect_response(is_list_api_response, timeout=api_timeout):
            await do_gesture()
        if SCROLL_AFTER_API_MS > 0:
            await page.wait_for_timeout(SCROLL_AFTER_API_MS + random.randint(0, 200))
        return
    except Exception as exc:
        if is_browser_dead(exc, page=page):
            raise
        name = type(exc).__name__
        if "Timeout" in name:
            # Gesture already ran; short human pause instead of another hard jump.
            await page.wait_for_timeout(random.randint(180, 450))
            return
        # Gesture may not have run if expect_response failed early — still scroll.
        try:
            await do_gesture()
        except Exception as inner:
            if is_browser_dead(inner, page=page):
                raise
    if SCROLL_FALLBACK_MS > 0:
        await page.wait_for_timeout(SCROLL_FALLBACK_MS + random.randint(0, 250))


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


def category_tab_display_name(tab: str) -> str:
    """Decode categoryTab token to a human label, e.g. toys_%26_games -> Toys & Games."""
    raw = unquote((tab or "").strip())
    if not raw:
        return ""
    text = raw.replace("_", " ").replace("%26", "&").replace("%2C", ",")
    text = re.sub(r"\s+", " ", text).strip()
    # Keep short connectors lowercase when title-casing.
    parts: list[str] = []
    for token in text.split(" "):
        low = token.lower()
        if low in {"&", "and", "or", "of", "the"} and parts:
            parts.append(low if low != "&" else "&")
        elif low == "&":
            parts.append("&")
        else:
            parts.append(token[:1].upper() + token[1:] if token else token)
    return " ".join(parts)


def build_calp_l1_url(site_base: str, category_tab: str) -> str:
    tab = unquote((category_tab or "").strip())
    base = (site_base or "https://www.aliexpress.us").rstrip("/")
    return f"{base}/p/calp-plus/index.html?categoryTab={quote(tab, safe='')}"


def build_l1_seed_docs(
    tabs: list[dict[str, str]],
    *,
    site_prefix: str = "US",
    site_base: str | None = None,
) -> list[dict]:
    """Build ES seed docs for discovered L1 calp categoryTab entries."""
    docs: list[dict] = []
    forced_base = (site_base or "").rstrip("/") or None
    for i, row in enumerate(tabs):
        tab = str(row.get("tab") or "").strip()
        url = str(row.get("url") or "").strip()
        label = str(row.get("name") or "").strip() or category_tab_display_name(tab)
        if not tab or not url:
            continue
        if forced_base:
            url = build_calp_l1_url(forced_base, tab)
        if is_blacklisted_category(name=label, url=url):
            continue
        host = site_host_from_url(url)
        if forced_base:
            prefix = "COM" if forced_base.endswith(".com") else "US"
        else:
            prefix = site_prefix
            if host.endswith(".com"):
                prefix = "COM"
            elif host.endswith(".us"):
                prefix = "US"
        name = f"{prefix} / {label}"
        docs.append(
            {
                "name": name,
                "display_name": label,
                "category_tab": tab.lower(),
                "url": with_listing_filters(url),
                "enabled": True,
                "priority": i + 1,
                "site": host,
                "seed_type": "calp_l1",
            }
        )
    return docs


async def list_calp_l1_tabs(
    page: Page, *, site_base: str | None = None
) -> list[dict[str, str]]:
    """Collect top-nav / sidebar L1 links that carry categoryTab=…."""
    rows = await page.evaluate(
        """
        () => {
          const out = [];
          const seen = new Set();
          const anchors = document.querySelectorAll('a[href*="categoryTab="]');
          for (const a of anchors) {
            const href = a.href || '';
            let tab = '';
            try {
              tab = new URL(href).searchParams.get('categoryTab') || '';
            } catch (e) {
              const m = href.match(/categoryTab=([^&]+)/);
              tab = m ? decodeURIComponent(m[1]) : '';
            }
            tab = (tab || '').trim();
            if (!tab) continue;
            const key = tab.toLowerCase();
            if (seen.has(key)) continue;
            seen.add(key);
            let name = (a.innerText || a.getAttribute('aria-label') || a.title || '')
              .trim().replace(/\\s+/g, ' ');
            if (!name || name.length > 80) {
              name = tab.replace(/_/g, ' ');
            }
            out.push({ name, tab, url: href.split('#')[0] });
          }
          return out;
        }
        """
    )
    cleaned: list[dict[str, str]] = []
    forced = (site_base or "").rstrip("/") or None
    for row in rows or []:
        tab = str(row.get("tab") or "").strip()
        url = str(row.get("url") or "").strip()
        name = str(row.get("name") or "").strip() or category_tab_display_name(tab)
        if not tab or not url:
            continue
        if is_blacklisted_category(name=name, url=url):
            continue
        # Prefer durable calp-plus form on the requested (or page) site.
        site = forced or site_base_from_url(url)
        cleaned.append(
            {
                "name": name,
                "tab": tab,
                "url": build_calp_l1_url(site, tab),
            }
        )
    return cleaned


async def list_category_href_links(page: Page) -> list[dict[str, str]]:
    """Collect classic /category/{id}/… listing links visible on the page."""
    rows = await page.evaluate(
        """
        () => {
          const out = [];
          const seen = new Set();
          for (const a of document.querySelectorAll('a[href*="/category/"]')) {
            const href = (a.href || '').split('#')[0];
            const m = href.match(/\\/category\\/(\\d+)\\/[^?]+\\.html/i);
            if (!m) continue;
            const id = m[1];
            if (seen.has(id)) continue;
            seen.add(id);
            const name = (a.innerText || a.getAttribute('title') || '')
              .trim().replace(/\\s+/g, ' ');
            if (!name || name.length < 2 || name.length > 80) continue;
            out.push({ name, url: href, category_id: id });
          }
          return out;
        }
        """
    )
    cleaned: list[dict[str, str]] = []
    for row in rows or []:
        name = str(row.get("name") or "").strip()
        url = str(row.get("url") or "").strip()
        cat_id = str(row.get("category_id") or "").strip()
        if not name or not url or not cat_id:
            continue
        if is_blacklisted_category(name=name, url=url):
            continue
        cleaned.append(
            {
                "name": name,
                "url": with_listing_filters(url),
                "category_id": cat_id,
            }
        )
    return cleaned


async def list_calp_lv3_names(page: Page) -> list[str]:
    names = await page.evaluate(
        """
        () => {
          const out = [];
          const seen = new Set();
          for (const el of document.querySelectorAll('[class*="lv3Category"]')) {
            // Ignore top L1 categoryTab links mistaken for lv3 chips.
            const link = el.closest('a[href*="categoryTab="]') || el.querySelector('a[href*="categoryTab="]');
            if (link) {
              const href = link.href || '';
              if (/categoryTab=women/i.test(href) || /categoryTab=men/i.test(href)) continue;
            }
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
    return [
        name
        for name in (names or [])
        if not is_blacklisted_category(name=name)
    ]


async def click_calp_lv3(page: Page, name: str) -> bool:
    await dismiss_popups(page)
    if is_blacklisted_category(name=name):
        return False
    return bool(
        await page.evaluate(
            """
            (targetName) => {
              const want = targetName.toLowerCase();
              const nodes = [...document.querySelectorAll(
                '[class*="lv3CategoryBox"], [class*="lv3Category"]'
              )];
              for (const el of nodes) {
                const link = el.closest('a[href*="categoryTab="]')
                  || el.querySelector('a[href*="categoryTab="]');
                if (link) {
                  const href = (link.href || '').toLowerCase();
                  if (href.includes('clothing') && (href.includes('women') || href.includes('men'))) {
                    continue;
                  }
                }
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
    worker_id: int = 0,
) -> list[tuple[str, str]]:
    if is_calp_url(category_url):
        # Calp lv3 icons have no href; handled inside crawl_category via clicks.
        return []

    parent_id = category_id_from_url(category_url)
    site_host = site_host_from_url(category_url)
    excluded = exclude_ids or set()
    filtered = with_listing_filters(category_url)
    await safe_goto(page, filtered, worker_id=worker_id)
    await dismiss_popups(page)
    await handle_captcha(page, worker_id=worker_id)
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
        subcategories.append(
            (f"{category_name} > {item['name']}", with_listing_filters(item["url"]))
        )
    return subcategories


async def discover_all_subcategories(
    page: Page,
    category_name: str,
    category_url: str,
    *,
    max_depth: int = MAX_SUBCATEGORY_DEPTH,
    worker_id: int = 0,
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
            worker_id=worker_id,
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
) -> tuple[int, int]:
    """Append new URLs locally and upsert to ES (all scraped ids).

    Quality gate only affects the returned quality_passed count (stats on the
    category seed); it does NOT skip ES / local file writes.

    Returns (new_count, quality_passed_count_this_batch).
    """
    new_count = 0
    quality_passed = 0
    LINKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with FILE_IO_LOCK:
        with LINKS_FILE.open("a", encoding="utf-8") as links_fh, PRODUCTS_JSONL.open(
            "a", encoding="utf-8"
        ) as jsonl_fh:
            for product in products:
                if product_passes_quality(product):
                    quality_passed += 1
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
                    "quality_passed": product_passes_quality(product),
                    "scraped_at": utc_now_iso(),
                }
                jsonl_fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                new_count += 1
    return new_count, quality_passed


def worker_user_data_dir(worker_id: int) -> Path:
    # Keep worker-0 on the legacy profile so existing cookies still apply.
    if CRAWL_WORKERS <= 1 or worker_id <= 0:
        return USER_DATA_DIR
    return USER_DATA_DIR / f"worker-{worker_id}"


async def detect_listing_load_mode(page: Page) -> str:
    """Inspect the live page: pagination controls → flip pages, else infinite scroll."""
    # Pager often sits below the fold on wholesale pages.
    try:
        await page.evaluate(
            """
            () => {
              const sh = Math.max(
                document.body ? document.body.scrollHeight : 0,
                document.documentElement ? document.documentElement.scrollHeight : 0
              );
              window.scrollTo(0, Math.max(0, sh - (window.innerHeight || 800)));
            }
            """
        )
        await page.wait_for_timeout(450)
    except Exception as exc:
        if is_browser_dead(exc, page=page):
            raise

    info = await page.evaluate(
        """
        () => {
          const visible = (el) => {
            if (!el) return false;
            const st = window.getComputedStyle(el);
            if (!st || st.display === 'none' || st.visibility === 'hidden') return false;
            const r = el.getBoundingClientRect();
            return r.width > 2 && r.height > 2;
          };
          const pagerSelectors = [
            '.comet-pagination',
            '[class*="Pagination"]',
            '[class*="pagination"]',
            'ul.pagination',
            'nav[aria-label*="agination" i]',
            '[data-spm*="pagination"]',
          ];
          let hasPager = false;
          for (const sel of pagerSelectors) {
            for (const el of document.querySelectorAll(sel)) {
              if (visible(el)) { hasPager = true; break; }
            }
            if (hasPager) break;
          }
          let pageLinks = 0;
          for (const a of document.querySelectorAll('a[href*="page="]')) {
            if (visible(a)) pageLinks += 1;
          }
          let hasNext = false;
          for (const el of document.querySelectorAll('a, button, li, span')) {
            if (!visible(el)) continue;
            const label = (
              el.getAttribute('aria-label') || el.getAttribute('title') || el.innerText || ''
            ).trim().toLowerCase();
            if (!label) continue;
            if (
              label === 'next' ||
              label === '>' ||
              label === '›' ||
              label.includes('next page') ||
              label === '下一页'
            ) {
              hasNext = true;
              break;
            }
          }
          return {
            hasPager,
            pageLinks,
            hasNext,
            hasPagination: hasPager || pageLinks >= 2 || hasNext,
          };
        }
        """
    )
    try:
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(150)
    except Exception as exc:
        if is_browser_dead(exc, page=page):
            raise

    has_dom = bool((info or {}).get("hasPagination"))
    mode = infer_listing_load_mode(
        has_pagination_dom=has_dom,
        url=page.url or "",
    )
    return mode


async def click_listing_next_page(page: Page) -> bool:
    """Click a visible Next / page-N control when present."""
    await dismiss_popups(page)
    return bool(
        await page.evaluate(
            """
            () => {
              const visible = (el) => {
                if (!el) return false;
                const st = window.getComputedStyle(el);
                if (!st || st.display === 'none' || st.visibility === 'hidden') return false;
                const r = el.getBoundingClientRect();
                return r.width > 2 && r.height > 2;
              };
              const candidates = [];
              for (const el of document.querySelectorAll(
                'a, button, li, span, div[role="button"]'
              )) {
                if (!visible(el)) continue;
                const label = (
                  el.getAttribute('aria-label') || el.getAttribute('title') || el.innerText || ''
                ).trim().toLowerCase();
                const href = (el.getAttribute('href') || '').toLowerCase();
                const cls = (el.className || '').toString().toLowerCase();
                const disabled =
                  el.getAttribute('aria-disabled') === 'true' ||
                  el.hasAttribute('disabled') ||
                  cls.includes('disabled') ||
                  cls.includes('is-disabled');
                if (disabled) continue;
                let score = 0;
                if (label === 'next' || label === '下一页' || label.includes('next page')) score = 5;
                else if (label === '>' || label === '›' || label === '»') score = 4;
                else if (cls.includes('next') && (href.includes('page=') || label)) score = 3;
                else if (/[?&]page=\\d+/.test(href) && /page=([2-9]|\\d{2,})/.test(href)) score = 2;
                if (score > 0) candidates.push({ el, score });
              }
              candidates.sort((a, b) => b.score - a.score);
              if (!candidates.length) return false;
              const target = candidates[0].el;
              target.scrollIntoView({ block: 'center' });
              target.click();
              return true;
            }
            """
        )
    )


async def _ingest_listing_products(
    page: Page,
    category_name: str,
    site_base: str,
    seen_links: set[str],
    collector: ListingCollector,
    es_writer: ElasticsearchUrlWriter | None,
    *,
    seen_ids: set[str],
    quality_ids: set[str],
    enriched_ids: set[str],
    metrics_by_id: dict[str, ListingProduct],
    worker_id: int,
    round_label: str,
) -> tuple[set[str], int, int]:
    """Extract / enrich / save one listing screen. Returns (current_ids, fresh, new_count)."""
    products = await extract_products(page, site_base, collector)
    collector.note_listing_total(await extract_listing_total_from_dom(page))

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
            and (
                p.rating is None
                or p.reviews is None
                or (QUALITY_FILTER and p.sold_count is None)
            )
        ]
        if pending:
            await enrich_rating_reviews(page, pending)
            enriched_ids.update(p.product_id for p in pending)

    for product in products:
        metrics_by_id[product.product_id] = product
        if product_passes_quality(product):
            quality_ids.add(product.product_id)

    new_count, _quality_n = save_new_products(
        products, seen_links, category_name, es_writer
    )
    if es_writer is not None:
        es_writer.flush()

    with_metrics = sum(
        1 for p in products if p.rating is not None and p.reviews is not None
    )
    total_hint = (
        f"，页面/API 宣称总数 {collector.listing_total}"
        if collector.listing_total is not None
        else ""
    )
    quality_hint = (
        f"，质量门累计通过 {len(quality_ids)}" if QUALITY_FILTER else ""
    )
    worker_log(
        worker_id,
        f"{round_label}：可见/API 合计 {len(products)} 个"
        f"（{with_metrics} 个含评分+评论数），"
        f"本轮新 ID {len(fresh)} 个，保存新增 {new_count} 个"
        f"{quality_hint}{total_hint}",
    )
    return current_ids, len(fresh), new_count


async def crawl_infinite_scroll(
    page: Page,
    category_name: str,
    site_base: str,
    seen_links: set[str],
    collector: ListingCollector,
    es_writer: ElasticsearchUrlWriter | None = None,
    *,
    worker_id: int = 0,
    listing_url: str | None = None,
) -> CategoryCrawlStats:
    """Scroll until consecutive rounds yield no new product IDs."""
    stats = CategoryCrawlStats()
    seen_ids: set[str] = set()
    quality_ids: set[str] = set()
    enriched_ids: set[str] = set()
    metrics_by_id: dict[str, ListingProduct] = {}
    consecutive_dup = 0
    round_no = 0

    while True:
        if MAX_SCROLL_ROUNDS > 0 and round_no >= MAX_SCROLL_ROUNDS:
            worker_log(worker_id, f"达到最大滚动轮数 {MAX_SCROLL_ROUNDS}，停止：{category_name}")
            break

        round_no += 1
        if round_no > 1:
            await scroll_listing_page(page)
        else:
            await dismiss_popups(page)
            if FIRST_ROUND_SETTLE_MS > 0:
                await page.wait_for_timeout(FIRST_ROUND_SETTLE_MS)

        if await is_login_page(page) and listing_url:
            worker_log(worker_id, "滚动中被踢到登录页，等待登录后重新打开列表。")
            await wait_until_unblocked(page, kind="登录", worker_id=worker_id)
            await safe_goto(page, listing_url, worker_id=worker_id)
        else:
            await handle_captcha(page, worker_id=worker_id)
            if listing_url and await is_login_page(page):
                await safe_goto(page, listing_url, worker_id=worker_id)

        current_ids, fresh_n, new_count = await _ingest_listing_products(
            page,
            category_name,
            site_base,
            seen_links,
            collector,
            es_writer,
            seen_ids=seen_ids,
            quality_ids=quality_ids,
            enriched_ids=enriched_ids,
            metrics_by_id=metrics_by_id,
            worker_id=worker_id,
            round_label=f"滚动第 {round_no} 轮",
        )
        stats.new_count += new_count

        if (not current_ids or fresh_n == 0) and round_no > 1:
            consecutive_dup += 1
        else:
            consecutive_dup = 0

        seen_ids.update(current_ids)

        if consecutive_dup >= CONSECUTIVE_DUPLICATE_SCROLLS_TO_STOP:
            worker_log(
                worker_id,
                f"连续 {consecutive_dup} 轮无新商品，判定 {category_name} 已到底。",
            )
            break

        await sleep(short=True)

    stats.product_count = len(seen_ids)
    stats.quality_passed = len(quality_ids)
    stats.listing_total = collector.listing_total
    return stats


async def crawl_paginated(
    page: Page,
    category_name: str,
    site_base: str,
    seen_links: set[str],
    collector: ListingCollector,
    es_writer: ElasticsearchUrlWriter | None = None,
    *,
    worker_id: int = 0,
    listing_url: str | None = None,
) -> CategoryCrawlStats:
    """Flip ?page=N (and click Next when needed) until no new product IDs."""
    stats = CategoryCrawlStats()
    seen_ids: set[str] = set()
    quality_ids: set[str] = set()
    enriched_ids: set[str] = set()
    metrics_by_id: dict[str, ListingProduct] = {}
    consecutive_dup = 0
    page_no = 1
    base_url = with_listing_filters(listing_url or page.url or "")

    while True:
        if MAX_PAGES_PER_CATEGORY > 0 and page_no > MAX_PAGES_PER_CATEGORY:
            worker_log(
                worker_id,
                f"达到最大翻页数 {MAX_PAGES_PER_CATEGORY}，停止：{category_name}",
            )
            break

        page_url = build_page_url(base_url, page_no)
        worker_log(worker_id, f"打开第 {page_no} 页：{page_url}")
        collector.clear()
        await safe_goto(page, page_url, worker_id=worker_id)
        await dismiss_popups(page)
        await handle_captcha(page, worker_id=worker_id)
        if await is_login_page(page):
            worker_log(worker_id, "翻页中被踢到登录页，等待登录后重开。")
            await wait_until_unblocked(page, kind="登录", worker_id=worker_id)
            await safe_goto(page, page_url, worker_id=worker_id)
        # Only click Orders on page 1 — re-clicking resets SPA back to page 1.
        if page_no == 1:
            await apply_orders_sort_ui(page, worker_id=worker_id)
        await sleep(short=page_no > 1)

        current_ids, fresh_n, new_count = await _ingest_listing_products(
            page,
            category_name,
            site_base,
            seen_links,
            collector,
            es_writer,
            seen_ids=seen_ids,
            quality_ids=quality_ids,
            enriched_ids=enriched_ids,
            metrics_by_id=metrics_by_id,
            worker_id=worker_id,
            round_label=f"第 {page_no} 页",
        )
        stats.new_count += new_count

        if not current_ids:
            worker_log(worker_id, f"第 {page_no} 页没有数据，判定到最后一页：{category_name}")
            break

        if page_no > 1 and fresh_n == 0:
            consecutive_dup += 1
            if consecutive_dup >= CONSECUTIVE_DUPLICATE_PAGES_TO_STOP:
                worker_log(
                    worker_id,
                    f"连续 {consecutive_dup} 页无新商品，停止翻页：{category_name}",
                )
                break
            # URL page= may be ignored by SPA — try clicking Next once.
            if await click_listing_next_page(page):
                worker_log(worker_id, f"第 {page_no} 页无新 ID，已点击 Next 控件重试")
                await page.wait_for_timeout(800)
                await handle_captcha(page, worker_id=worker_id)
                current_ids2, fresh_n2, new_count2 = await _ingest_listing_products(
                    page,
                    category_name,
                    site_base,
                    seen_links,
                    collector,
                    es_writer,
                    seen_ids=seen_ids,
                    quality_ids=quality_ids,
                    enriched_ids=enriched_ids,
                    metrics_by_id=metrics_by_id,
                    worker_id=worker_id,
                    round_label=f"第 {page_no} 页(Next)",
                )
                stats.new_count += new_count2
                current_ids = current_ids | current_ids2
                if fresh_n2 > 0:
                    consecutive_dup = 0
                    seen_ids.update(current_ids)
                    page_no += 1
                    await sleep()
                    continue
        else:
            consecutive_dup = 0

        seen_ids.update(current_ids)
        page_no += 1
        await sleep()

    stats.product_count = len(seen_ids)
    stats.quality_passed = len(quality_ids)
    stats.listing_total = collector.listing_total
    return stats


async def crawl_listing(
    page: Page,
    category_name: str,
    site_base: str,
    seen_links: set[str],
    collector: ListingCollector,
    es_writer: ElasticsearchUrlWriter | None = None,
    *,
    worker_id: int = 0,
    listing_url: str | None = None,
) -> CategoryCrawlStats:
    """Auto-detect pagination vs infinite scroll, then crawl accordingly."""
    mode = await detect_listing_load_mode(page)
    worker_log(
        worker_id,
        f"列表加载方式：{'翻页' if mode == 'pagination' else '无限下滑'} — {category_name}",
    )
    if mode == "pagination":
        return await crawl_paginated(
            page,
            category_name,
            site_base,
            seen_links,
            collector,
            es_writer,
            worker_id=worker_id,
            listing_url=listing_url,
        )
    return await crawl_infinite_scroll(
        page,
        category_name,
        site_base,
        seen_links,
        collector,
        es_writer,
        worker_id=worker_id,
        listing_url=listing_url,
    )


async def _prepare_calp_lv3(
    page: Page, category_url: str, sub_name: str, *, worker_id: int = 0
) -> bool:
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
    await safe_goto(page, category_url, worker_id=worker_id)
    await dismiss_popups(page)
    await handle_captcha(page, worker_id=worker_id)
    await page.wait_for_timeout(500)
    if not await click_calp_lv3(page, sub_name):
        return False
    if CALP_CLICK_SETTLE_MS > 0:
        await page.wait_for_timeout(CALP_CLICK_SETTLE_MS)
    return True


def build_subcategory_seed_docs(
    parent_name: str,
    parent_url: str,
    lv3_names: list[str],
    *,
    parent_priority: int | None = None,
) -> list[dict]:
    """Build ES seed docs for calp lv3 chips.

    Each seed gets:
    - a wholesale URL (SortType=total_tranpro_desc works reliably)
    - a calp URL with calpLv3=… (for in-page chip click fallback)
    """
    site_base = site_base_from_url(parent_url)
    base_priority = int(parent_priority or 100)
    docs: list[dict] = []
    for i, lv3 in enumerate(lv3_names):
        if is_blacklisted_category(name=lv3):
            continue
        name = f"{parent_name} > {lv3}"
        wholesale = wholesale_search_url(lv3, site_base)
        calp = with_listing_filters(with_calp_lv3(parent_url, lv3))
        docs.append(
            {
                "name": name,
                "display_name": lv3,
                "url": wholesale,
                "calp_url": calp,
                "parent_name": parent_name,
                "parent_url": with_listing_filters(parent_url),
                "seed_type": "calp_lv3_wholesale",
                "enabled": True,
                "priority": base_priority * 1000 + i + 1,
                "site": site_host_from_url(parent_url),
            }
        )
    return docs


async def discover_calp_lv3_seed_docs(
    page: Page,
    category_name: str,
    category_url: str,
    *,
    worker_id: int = 0,
    parent_priority: int | None = None,
) -> list[dict]:
    """Open a calp L1 page, list lv3 chips, return ES seed docs."""
    if not is_calp_url(category_url):
        return []
    filtered = with_listing_filters(category_url)
    # Strip calpLv3 for discovery of the parent tab.
    parts = urlsplit(filtered)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.pop("calpLv3", None)
    filtered = urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )
    await safe_goto(page, filtered, worker_id=worker_id)
    await dismiss_popups(page)
    await handle_captcha(page, worker_id=worker_id)
    await page.wait_for_timeout(800)
    lv3_names = await list_calp_lv3_names(page)
    if not lv3_names:
        await safe_goto(page, filtered, worker_id=worker_id)
        await dismiss_popups(page)
        await handle_captcha(page, worker_id=worker_id)
        await page.wait_for_timeout(800)
        lv3_names = await list_calp_lv3_names(page)
    if not lv3_names:
        return []
    worker_log(
        worker_id,
        f"发现 {len(lv3_names)} 个 calp 子类目 → ES seeds：{category_name}",
    )
    return build_subcategory_seed_docs(
        category_name,
        category_url,
        lv3_names,
        parent_priority=parent_priority,
    )


async def crawl_category(
    page: Page,
    category_name: str,
    category_url: str,
    seen_links: set[str],
    collector: ListingCollector,
    es_writer: ElasticsearchUrlWriter | None = None,
    worker_id: int = 0,
    *,
    claim_client: CategoryClaimClient | None = None,
) -> CategoryCrawlStats:
    stats = CategoryCrawlStats()
    category_url = with_listing_filters(category_url)
    site_base = site_base_from_url(category_url)
    lv3_target = calp_lv3_query_name(category_url)
    worker_log(worker_id, f"类目：{category_name}")
    worker_log(worker_id, f"打开：{category_url}")

    def _merge(part: CategoryCrawlStats) -> None:
        stats.new_count += part.new_count
        stats.product_count += part.product_count
        stats.quality_passed += part.quality_passed
        if part.listing_total is not None:
            if stats.listing_total is None or part.listing_total > stats.listing_total:
                stats.listing_total = part.listing_total

    collector.clear()
    await safe_goto(page, category_url, worker_id=worker_id)
    await dismiss_popups(page)
    await handle_captcha(page, worker_id=worker_id)
    await apply_orders_sort_ui(page, worker_id=worker_id)
    await sleep()

    # If this seed encodes a calp lv3 chip, click it before scrolling.
    if is_calp_url(category_url) and lv3_target:
        parent_for_click = category_url
        if not await _prepare_calp_lv3(
            page, parent_for_click, lv3_target, worker_id=worker_id
        ):
            worker_log(worker_id, f"未能点击 calpLv3={lv3_target}，继续滚当前页")
        else:
            await apply_orders_sort_ui(page, worker_id=worker_id)

    _merge(
        await crawl_listing(
            page,
            category_name,
            site_base,
            seen_links,
            collector,
            es_writer,
            worker_id=worker_id,
            listing_url=category_url,
        )
    )

    # Calp L1: discover lv3 → upsert ES seeds (claimable separately). Optionally
    # still inline-crawl lv3 when SUBCATEGORY_AS_SEEDS is off.
    if (
        is_calp_url(category_url)
        and CRAWL_SUBCATEGORIES
        and " > " not in category_name
        and not lv3_target
    ):
        try:
            await page.evaluate("window.scrollTo(0, 0)")
        except Exception:
            pass
        await page.wait_for_timeout(500)
        lv3_names = await list_calp_lv3_names(page)
        if not lv3_names:
            await safe_goto(page, category_url, worker_id=worker_id)
            await dismiss_popups(page)
            await handle_captcha(page, worker_id=worker_id)
            await page.wait_for_timeout(800)
            lv3_names = await list_calp_lv3_names(page)
        if lv3_names:
            worker_log(
                worker_id,
                f"发现 {len(lv3_names)} 个 calp 子类目图标：{category_name}",
            )
            if SUBCATEGORY_AS_SEEDS and claim_client is not None:
                docs = build_subcategory_seed_docs(
                    category_name, category_url, lv3_names
                )
                n = await asyncio.to_thread(claim_client.upsert_seeds, docs)
                worker_log(
                    worker_id,
                    f"已写入 ES 子类目 seeds {n}/{len(docs)}：{category_name}",
                )
            elif not SUBCATEGORY_AS_SEEDS:
                for sub_name in lv3_names:
                    if is_blacklisted_category(name=sub_name):
                        worker_log(worker_id, f"跳过黑名单子类目：{sub_name}")
                        continue
                    target_name = f"{category_name} > {sub_name}"
                    worker_log(worker_id, f"子类目（点击）：{target_name}")
                    if CALP_RELOAD_EACH_LV3:
                        await safe_goto(page, category_url, worker_id=worker_id)
                        await dismiss_popups(page)
                        await handle_captcha(page, worker_id=worker_id)
                        await page.wait_for_timeout(500)
                        if not await click_calp_lv3(page, sub_name):
                            worker_log(worker_id, f"未能点击子类目：{sub_name}")
                            continue
                        if CALP_CLICK_SETTLE_MS > 0:
                            await page.wait_for_timeout(CALP_CLICK_SETTLE_MS)
                        await ensure_seed_category_tab(
                            page, category_url, worker_id=worker_id
                        )
                    elif not await _prepare_calp_lv3(
                        page, category_url, sub_name, worker_id=worker_id
                    ):
                        worker_log(worker_id, f"未能点击子类目：{sub_name}")
                        continue
                    await ensure_seed_category_tab(
                        page, category_url, worker_id=worker_id
                    )
                    await apply_orders_sort_ui(page, worker_id=worker_id)
                    if is_blocked_category_url(page.url or ""):
                        worker_log(
                            worker_id, f"点子类后落到禁止类目，跳过：{page.url}"
                        )
                        await safe_goto(page, category_url, worker_id=worker_id)
                        continue
                    collector.clear()
                    _merge(
                        await crawl_listing(
                            page,
                            target_name,
                            site_base,
                            seen_links,
                            collector,
                            es_writer,
                            worker_id=worker_id,
                            listing_url=category_url,
                        )
                    )

    elif (not is_calp_url(category_url)) and " > " not in category_name:
        wholesale = wholesale_search_url(category_name, site_base)
        worker_log(worker_id, f"附加批发搜索：{wholesale}")
        collector.clear()
        await safe_goto(page, wholesale, worker_id=worker_id)
        await dismiss_popups(page)
        await handle_captcha(page, worker_id=worker_id)
        await apply_orders_sort_ui(page, worker_id=worker_id)
        _merge(
            await crawl_listing(
                page,
                category_name,
                site_base,
                seen_links,
                collector,
                es_writer,
                worker_id=worker_id,
                listing_url=wholesale,
            )
        )

    return stats


async def create_browser(
    playwright, worker_id: int = 0, *, workers: int | None = None
) -> tuple[BrowserContext, Page, ListingCollector]:
    profile_dir = worker_user_data_dir(worker_id)
    profile_dir.mkdir(parents=True, exist_ok=True)
    collector = ListingCollector()
    worker_count = max(1, workers if workers is not None else CRAWL_WORKERS)
    launch_args = list(CHROMIUM_ARGS_BASE)
    x = y = width = height = 0
    if not HEADLESS:
        x, y, width, height = worker_window_bounds(worker_id, worker_count)
        launch_args.extend(
            [
                f"--window-position={x},{y}",
                f"--window-size={width},{height}",
            ]
        )
        worker_log(
            worker_id,
            f"窗口平铺: +{x}+{y} {width}x{height}（共 {worker_count} 格）",
        )
    else:
        launch_args.append("--start-maximized")

    context = await playwright.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir),
        headless=HEADLESS,
        user_agent=USER_AGENT,
        locale="en-US",
        viewport=None,
        no_viewport=True,
        args=launch_args,
        ignore_default_args=["--enable-automation"],
    )
    await context.add_init_script(STEALTH_SCRIPT)
    page = context.pages[0] if context.pages else await context.new_page()
    if not HEADLESS:
        await apply_window_bounds(page, x, y, width, height)

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


async def process_category_seed(
    playwright,
    worker_id: int,
    category_name: str,
    category_url: str,
    seen_links: set[str],
    es_writer: ElasticsearchUrlWriter,
    *,
    workers: int = 1,
    claim_client: CategoryClaimClient | None = None,
    claim_doc_id: str | None = None,
) -> CategoryCrawlStats:
    """Discover classic subcats (if needed) and crawl one seed category tree."""
    stats = CategoryCrawlStats()
    context: BrowserContext | None = None
    page: Page | None = None
    collector: ListingCollector | None = None
    targets = [(category_name, category_url)]
    heartbeat_task: asyncio.Task | None = None

    async def ensure_browser() -> tuple[BrowserContext, Page, ListingCollector]:
        nonlocal context, page, collector
        if context is None or page is None or page.is_closed():
            await close_browser(context)
            context, page, collector = await create_browser(
                playwright, worker_id, workers=workers
            )
        assert collector is not None
        return context, page, collector

    async def heartbeat_loop() -> None:
        if claim_client is None or not claim_doc_id:
            return
        while True:
            await asyncio.sleep(CLAIM_HEARTBEAT_SECONDS)
            ok = await asyncio.to_thread(claim_client.heartbeat, claim_doc_id)
            if ok:
                worker_log(worker_id, f"续租类目：{category_name}")
            else:
                worker_log(worker_id, f"续租失败（可能租约丢失）：{category_name}")

    try:
        if claim_client is not None and claim_doc_id:
            heartbeat_task = asyncio.create_task(heartbeat_loop())

        # Classic /category/ seeds still auto-discover linked subcats.
        # Calp-plus lv3 icons become ES seeds inside crawl_category when enabled.
        if (
            CRAWL_SUBCATEGORIES
            and not is_calp_url(category_url)
            and category_path_depth(category_name) < 3
        ):
            for attempt in range(2):
                try:
                    _, page, _ = await ensure_browser()
                    subs = await discover_all_subcategories(
                        page,
                        category_name,
                        category_url,
                        worker_id=worker_id,
                    )
                    if subs:
                        worker_log(
                            worker_id,
                            f"发现 {len(subs)} 个子类目（含多级）：{category_name}",
                        )
                        if SUBCATEGORY_AS_SEEDS and claim_client is not None:
                            docs = []
                            for i, (sub_name, sub_url) in enumerate(subs):
                                docs.append(
                                    {
                                        "name": sub_name,
                                        "url": with_listing_filters(sub_url),
                                        "parent_name": category_name,
                                        "parent_url": with_listing_filters(category_url),
                                        "seed_type": "category_href",
                                        "enabled": True,
                                        "priority": 500000 + i,
                                        "site": site_host_from_url(category_url),
                                    }
                                )
                            n = await asyncio.to_thread(
                                claim_client.upsert_seeds, docs
                            )
                            worker_log(
                                worker_id,
                                f"已写入 ES 子类目 seeds {n}/{len(docs)}",
                            )
                        else:
                            targets.extend(subs)
                    break
                except Exception as exc:
                    if is_browser_dead(exc, page=page) and attempt == 0:
                        worker_log(worker_id, f"子类目发现时浏览器异常，重启：{exc}")
                        await close_browser(context)
                        context, page, collector = None, None, None
                        await asyncio.sleep(3)
                        continue
                    worker_log(worker_id, f"子类目发现失败 {category_name}: {exc}")
                    break

        for target_name, target_url in targets:
            for attempt in range(2):
                try:
                    _, page, collector = await ensure_browser()
                    part = await crawl_category(
                        page,
                        target_name,
                        target_url,
                        seen_links,
                        collector,
                        es_writer,
                        worker_id=worker_id,
                        claim_client=claim_client,
                    )
                    stats.new_count += part.new_count
                    stats.product_count += part.product_count
                    stats.quality_passed += part.quality_passed
                    if part.listing_total is not None:
                        if (
                            stats.listing_total is None
                            or part.listing_total > stats.listing_total
                        ):
                            stats.listing_total = part.listing_total
                    break
                except Exception as exc:
                    if is_browser_dead(exc, page=page) and attempt == 0:
                        worker_log(worker_id, f"抓取时浏览器异常，重启：{exc}")
                        await close_browser(context)
                        context, page, collector = None, None, None
                        await asyncio.sleep(3)
                        continue
                    raise
    finally:
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
        await close_browser(context)

    return stats


def build_claim_client() -> CategoryClaimClient | None:
    if not CATEGORY_CLAIM_MODE:
        return None
    es_index = (os.getenv("ELASTICSEARCH_INDEX_CATEGORIES") or "").strip()
    es_url = (os.getenv("ELASTICSEARCH_URL") or "").strip()
    if not es_index or not es_url:
        return None
    enabled_only = (os.getenv("CATEGORIES_ENABLED_ONLY") or "1").strip().lower() not in {
        "0",
        "false",
        "no",
    }
    client = CategoryClaimClient(
        es_url,
        es_index,
        device_id=DEVICE_ID,
        lease_seconds=CLAIM_LEASE_SECONDS,
        reclaim_done=CRAWL_RECLAIM_DONE,
        enabled_only=enabled_only,
    )
    return client if client.enabled else None


async def crawl_worker(
    playwright,
    worker_id: int,
    category_queue: asyncio.Queue,
    seen_links: set[str],
    es_writer: ElasticsearchUrlWriter,
    *,
    workers: int = 1,
    claim_client: CategoryClaimClient | None = None,
    refill_lock: asyncio.Lock | None = None,
    claim_batch_size: int = 1,
    exhausted: dict | None = None,
) -> CategoryCrawlStats:
    totals = CategoryCrawlStats()
    # Stagger launches so windows tile cleanly and logins aren't all at once.
    if worker_id > 0:
        await asyncio.sleep(1.2 * worker_id)
    worker_log(
        worker_id,
        f"启动，浏览器目录: {worker_user_data_dir(worker_id)}",
    )

    async def next_item():
        """Get next category; in claim mode refill a batch when local queue is empty."""
        if claim_client is None or refill_lock is None or exhausted is None:
            return await category_queue.get()

        while True:
            try:
                return category_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            async with refill_lock:
                if exhausted.get("done"):
                    return None
                try:
                    return category_queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                batch = await asyncio.to_thread(
                    claim_client.claim_batch, claim_batch_size
                )
                if not batch:
                    exhausted["done"] = True
                    worker_log(worker_id, "ES 无可认领类目，本机收工。")
                    return None
                names = ", ".join(c.name for c in batch)
                print(
                    f"[claim] device={claim_client.device_id} "
                    f"随机认领 {len(batch)} 个：{names}"
                )
                for cat in batch:
                    await category_queue.put(cat)
                return category_queue.get_nowait()

    while True:
        item = await next_item()
        try:
            if item is None:
                return totals

            claim_doc_id: str | None = None
            if isinstance(item, ClaimedCategory):
                category_name, category_url = item.name, item.url
                claim_doc_id = item.doc_id
            else:
                category_name, category_url = item

            worker_log(worker_id, f"领取类目：{category_name}")
            try:
                stats = await process_category_seed(
                    playwright,
                    worker_id,
                    category_name,
                    category_url,
                    seen_links,
                    es_writer,
                    workers=workers,
                    claim_client=claim_client,
                    claim_doc_id=claim_doc_id,
                )
                totals.new_count += stats.new_count
                totals.product_count += stats.product_count
                totals.quality_passed += stats.quality_passed
                if claim_client is not None and claim_doc_id:
                    await asyncio.to_thread(
                        claim_client.complete,
                        claim_doc_id,
                        product_count=stats.product_count,
                        new_count=stats.new_count,
                        listing_total=stats.listing_total,
                        quality_passed=stats.quality_passed,
                        crawl_round=CURRENT_CRAWL_ROUND or None,
                        crawl_url=with_listing_filters(category_url),
                    )
                total_hint = (
                    f"，页面宣称 {stats.listing_total}"
                    if stats.listing_total is not None
                    else ""
                )
                quality_hint = (
                    f"，质量通过 {stats.quality_passed}"
                    if QUALITY_FILTER
                    else ""
                )
                worker_log(
                    worker_id,
                    f"完成类目：{category_name}（本类目 ID {stats.product_count}，"
                    f"新增 {stats.new_count}{quality_hint}{total_hint}；"
                    f"累计新增 {totals.new_count}）",
                )
            except Exception as exc:
                worker_log(worker_id, f"类目失败：{category_name}: {exc}")
                if claim_client is not None and claim_doc_id:
                    await asyncio.to_thread(
                        claim_client.fail, claim_doc_id, str(exc)
                    )
                # Keep other workers alive; failed seed is marked for reclaim.
                continue
        finally:
            if claim_client is None:
                category_queue.task_done()


async def run_one_crawl_pass(
    playwright,
    *,
    claim_client: CategoryClaimClient | None,
    categories: list[tuple[str, str]],
    seen_links: set[str],
    es_writer: ElasticsearchUrlWriter,
    workers: int,
    batch_size: int,
) -> CategoryCrawlStats:
    """Run workers until local queue / ES claims are exhausted."""
    category_queue: asyncio.Queue = asyncio.Queue()
    refill_lock = asyncio.Lock()
    exhausted = {"done": False}
    claim_mode = claim_client is not None

    if claim_mode:
        assert claim_client is not None
        first = await asyncio.to_thread(claim_client.claim_batch, batch_size)
        if not first:
            print("ES 无可认领类目（均被占用租约中，或未 enabled）。")
            return CategoryCrawlStats()
        print(
            f"[claim] device={claim_client.device_id} 首批随机认领 {len(first)} 个："
            + ", ".join(c.name for c in first)
        )
        for cat in first:
            await category_queue.put(cat)
    else:
        shuffled = list(categories)
        random.shuffle(shuffled)
        for item in shuffled:
            await category_queue.put(item)
        for _ in range(workers):
            await category_queue.put(None)
        for _ in range(workers):
            await category_queue.put(None)

    results = await asyncio.gather(
        *[
            crawl_worker(
                playwright,
                worker_id,
                category_queue,
                seen_links,
                es_writer,
                workers=workers,
                claim_client=claim_client if claim_mode else None,
                refill_lock=refill_lock if claim_mode else None,
                claim_batch_size=batch_size,
                exhausted=exhausted if claim_mode else None,
            )
            for worker_id in range(workers)
        ]
    )
    totals = CategoryCrawlStats()
    for part in results:
        totals.new_count += part.new_count
        totals.product_count += part.product_count
        totals.quality_passed += part.quality_passed
    return totals


async def bootstrap_subcategory_seeds(
    playwright,
    claim_client: CategoryClaimClient,
    *,
    workers: int = 1,
) -> int:
    """Open each L1 calp seed once and upsert lv3 wholesale URLs into ES."""
    if not SUBCATEGORY_AS_SEEDS or not CRAWL_SUBCATEGORIES:
        return 0
    seeds: list[dict] = []
    try:
        assert claim_client.client is not None
        resp = claim_client.client.search(
            index=claim_client.index,
            size=200,
            query={
                "bool": {
                    "must": [{"term": {"enabled": True}}],
                    "must_not": [
                        {"ids": {"values": ["__crawl_round__"]}},
                        {"wildcard": {"name": "* > *"}},
                    ],
                }
            },
            sort=[{"priority": {"order": "asc", "unmapped_type": "long"}}],
            _source=["name", "url", "priority"],
        )
        for hit in resp.get("hits", {}).get("hits", []):
            src = hit.get("_source") or {}
            name = str(src.get("name") or "").strip()
            url = str(src.get("url") or "").strip()
            if name and url and is_calp_url(url) and " > " not in name:
                seeds.append(
                    {
                        "name": name,
                        "url": url,
                        "priority": src.get("priority"),
                    }
                )
    except Exception as exc:
        print(f"bootstrap 读取 L1 seeds 失败: {exc}")
        return 0

    if not seeds:
        print("bootstrap: 无 L1 calp seeds，跳过子类目发现。")
        return 0

    context, page, _collector = await create_browser(playwright, 0, workers=workers)
    total = 0
    try:
        for seed in seeds:
            try:
                docs = await discover_calp_lv3_seed_docs(
                    page,
                    seed["name"],
                    seed["url"],
                    worker_id=0,
                    parent_priority=int(seed.get("priority") or 100),
                )
                if docs:
                    n = await asyncio.to_thread(claim_client.upsert_seeds, docs)
                    total += n
                    print(f"bootstrap {seed['name']}: upsert {n}/{len(docs)} 子类目")
            except Exception as exc:
                print(f"bootstrap 失败 {seed['name']}: {exc}")
                if is_browser_dead(exc, page=page):
                    await close_browser(context)
                    context, page, _collector = await create_browser(
                        playwright, 0, workers=workers
                    )
    finally:
        await close_browser(context)
    return total


def write_discovered_category_files(
    docs: list[dict],
    *,
    yaml_path: Path,
    jsonl_path: Path,
) -> None:
    """Persist discovered seeds to YAML (L1) + JSONL (all)."""
    yaml_path.parent.mkdir(parents=True, exist_ok=True)
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    l1 = [
        {"name": d["name"], "url": d["url"]}
        for d in docs
        if d.get("seed_type") == "calp_l1" and d.get("name") and d.get("url")
    ]
    # Prefer L1 list for YAML; if empty, fall back to any top-level (no ' > ').
    if not l1:
        l1 = [
            {"name": d["name"], "url": d["url"]}
            for d in docs
            if d.get("name")
            and d.get("url")
            and " > " not in str(d.get("name") or "")
        ]
    lines = [
        "# Auto-generated by discover_categories — do not edit by hand.",
        f"# Generated at {utc_now_iso()}",
        "categories:",
    ]
    for item in l1:
        lines.append(f"  - name: {item['name']}")
        lines.append(f"    url: {item['url']}")
        lines.append("")
    yaml_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    with jsonl_path.open("w", encoding="utf-8") as fh:
        for doc in docs:
            fh.write(json.dumps(doc, ensure_ascii=False) + "\n")


async def run_category_url_discovery(
    playwright,
    *,
    site_base: str = "https://www.aliexpress.us",
    claim_client: CategoryClaimClient | None = None,
    include_lv3: bool = True,
    include_category_hrefs: bool = True,
    yaml_out: Path | None = None,
    jsonl_out: Path | None = None,
    workers: int = 1,
) -> dict[str, int]:
    """Crawl calp L1 tabs (+ optional lv3 /category links) and upsert ES seeds.

    This is the pre-product step: only category URLs, no listing scroll.
    """
    site_base = (site_base or "https://www.aliexpress.us").rstrip("/")
    yaml_path = yaml_out or (BASE_DIR / "config" / "categories.discovered.yaml")
    jsonl_path = jsonl_out or (BASE_DIR / "data" / "category_urls.jsonl")
    start_url = f"{site_base}/p/calp-plus/index.html"

    print("=" * 60)
    print("AliExpress 类目 URL 发现 (discover_categories)")
    print("=" * 60)
    print(f"站点: {site_base}")
    print(f"入口: {start_url}")
    print(f"子类目 lv3: {'是' if include_lv3 else '否'}")
    print(f"/category/ 链接: {'是' if include_category_hrefs else '否'}")
    if claim_client is not None:
        print(f"ES 类目索引: {claim_client.index}")
    else:
        print("ES 类目索引: 关闭（仅写本地文件）")
    print(f"YAML: {yaml_path}")
    print(f"JSONL: {jsonl_path}")
    print()

    context, page, _collector = await create_browser(playwright, 0, workers=workers)
    all_docs: list[dict] = []
    stats = {"l1": 0, "lv3": 0, "category_href": 0, "upserted": 0}
    try:
        await safe_goto(page, start_url, worker_id=0)
        await dismiss_popups(page)
        await handle_captcha(page, worker_id=0)
        await page.wait_for_timeout(1200)

        l1_tabs = await list_calp_l1_tabs(page, site_base=site_base)
        if not l1_tabs:
            # Some sessions land without nav; open a known tab then re-scan.
            fallback = build_calp_l1_url(site_base, "automotive")
            print(f"首屏未发现 L1 tabs，尝试打开 {fallback}", flush=True)
            await safe_goto(page, fallback, worker_id=0)
            await dismiss_popups(page)
            await handle_captcha(page, worker_id=0)
            await page.wait_for_timeout(1200)
            l1_tabs = await list_calp_l1_tabs(page, site_base=site_base)

        l1_docs = build_l1_seed_docs(l1_tabs, site_base=site_base)
        all_docs.extend(l1_docs)
        stats["l1"] = len(l1_docs)
        print(f"发现 L1 类目 {len(l1_docs)} 个（黑名单已过滤）", flush=True)
        for doc in l1_docs:
            print(f"  - {doc['name']} -> {doc['category_tab']}", flush=True)

        if claim_client is not None and l1_docs:
            n = await asyncio.to_thread(claim_client.upsert_seeds, l1_docs)
            stats["upserted"] += n
            print(f"已写入 ES L1 seeds {n}/{len(l1_docs)}", flush=True)

        for idx, doc in enumerate(l1_docs, start=1):
            name, url = doc["name"], doc["url"]
            priority = int(doc.get("priority") or 100)
            print(f"[{idx}/{len(l1_docs)}] 展开子类目：{name}", flush=True)
            try:
                if include_lv3:
                    lv3_docs = await discover_calp_lv3_seed_docs(
                        page,
                        name,
                        url,
                        worker_id=0,
                        parent_priority=priority,
                    )
                    if lv3_docs:
                        all_docs.extend(lv3_docs)
                        stats["lv3"] += len(lv3_docs)
                        print(f"  {name}: lv3 {len(lv3_docs)}", flush=True)
                        if claim_client is not None:
                            n = await asyncio.to_thread(
                                claim_client.upsert_seeds, lv3_docs
                            )
                            stats["upserted"] += n
                    else:
                        print(f"  {name}: lv3 0", flush=True)
                else:
                    await safe_goto(page, with_listing_filters(url), worker_id=0)
                    await dismiss_popups(page)
                    await handle_captcha(page, worker_id=0)
                    await page.wait_for_timeout(800)

                if include_category_hrefs:
                    href_rows = await list_category_href_links(page)
                    href_docs: list[dict] = []
                    for i, row in enumerate(href_rows):
                        child_name = f"{name} > {row['name']}"
                        if is_blacklisted_category(name=child_name, url=row["url"]):
                            continue
                        href_docs.append(
                            {
                                "name": child_name,
                                "display_name": row["name"],
                                "url": row["url"],
                                "parent_name": name,
                                "parent_url": with_listing_filters(url),
                                "category_id": row.get("category_id"),
                                "seed_type": "category_href",
                                "enabled": True,
                                "priority": priority * 1000 + 500 + i,
                                "site": site_host_from_url(url),
                            }
                        )
                    if href_docs:
                        all_docs.extend(href_docs)
                        stats["category_href"] += len(href_docs)
                        print(f"  {name}: /category/ {len(href_docs)}", flush=True)
                        if claim_client is not None:
                            n = await asyncio.to_thread(
                                claim_client.upsert_seeds, href_docs
                            )
                            stats["upserted"] += n
            except Exception as exc:
                import traceback

                print(f"发现失败 {name}: {exc}", flush=True)
                traceback.print_exc()
                if is_browser_dead(exc, page=page):
                    await close_browser(context)
                    context, page, _collector = await create_browser(
                        playwright, 0, workers=workers
                    )
    except Exception as exc:
        import traceback

        print(f"类目发现中断: {exc}", flush=True)
        traceback.print_exc()
    finally:
        await close_browser(context)

    # De-dupe by name keeping first (also on partial failure).
    deduped: list[dict] = []
    seen_names: set[str] = set()
    for doc in all_docs:
        key = str(doc.get("name") or "").strip()
        if not key or key in seen_names:
            continue
        seen_names.add(key)
        deduped.append(doc)

    write_discovered_category_files(deduped, yaml_path=yaml_path, jsonl_path=jsonl_path)
    print()
    print(
        f"完成：L1={stats['l1']} lv3={stats['lv3']} "
        f"category_href={stats['category_href']} "
        f"ES写入≈{stats['upserted']} 本地={len(deduped)}",
        flush=True,
    )
    print(f"YAML -> {yaml_path}", flush=True)
    print(f"JSONL -> {jsonl_path}", flush=True)
    return stats


async def main_async() -> None:
    global CURRENT_CRAWL_ROUND
    claim_client = build_claim_client()
    claim_mode = claim_client is not None
    device = default_device_id(DEVICE_ID)
    batch_size = CLAIM_BATCH_SIZE if CLAIM_BATCH_SIZE > 0 else CRAWL_WORKERS

    categories: list[tuple[str, str]] = []
    if not claim_mode:
        categories = load_categories()
        workers = min(CRAWL_WORKERS, max(1, len(categories)))
    else:
        workers = max(1, CRAWL_WORKERS)

    print("=" * 60)
    print("AliExpress 商品链接抓取 (alilj.py)")
    print("=" * 60)
    print(f"并行 worker: {workers}")
    print(f"浏览器状态目录: {USER_DATA_DIR}")
    if workers > 1:
        print(f"  worker-0 -> {USER_DATA_DIR.name}/，其余 -> worker-1…worker-{workers - 1}/")
    if not HEADLESS:
        sw, sh = detect_screen_size()
        print(f"窗口平铺: 屏幕 {sw}x{sh}，按 {workers} 格排布（不互相最大化遮盖）")
    print(f"登录恢复重试: 最多 {LOGIN_RECOVERY_RETRIES} 次")
    print(f"商品链接文件: {LINKS_FILE}")
    print(f"商品列表文件: {PRODUCTS_JSONL}")
    if claim_mode:
        assert claim_client is not None
        print(
            f"多设备认领: 开启（device={claim_client.device_id}，"
            f"batch={batch_size}，lease={CLAIM_LEASE_SECONDS}s）"
        )
        print(f"类目索引: {claim_client.index}")
        print(f"重抓已完成: {'是' if CRAWL_RECLAIM_DONE else '否'}")
        print(f"循环抓取: {'是' if CRAWL_LOOP else '否'}（间隔 {CRAWL_LOOP_SLEEP_SEC}s）")
        print(f"子类目入库: {'是' if SUBCATEGORY_AS_SEEDS else '否'}")
    else:
        print(f"多设备认领: 关闭（本地队列，共 {len(categories)} 个类目）")
        print(f"类目数量: {len(categories)}（US）")
    print(f"子类目发现: {'开启' if CRAWL_SUBCATEGORIES else '关闭'}")
    if CRAWL_SUBCATEGORIES:
        print(f"子类目最大深度: {MAX_SUBCATEGORY_DEPTH}")
        print(f"calp 每子类重开: {'开启' if CALP_RELOAD_EACH_LV3 else '关闭（页内点击）'}")
    print(
        f"下滑停止阈值: 连续 {CONSECUTIVE_DUPLICATE_SCROLLS_TO_STOP} 轮无新商品；"
        f"翻页停止阈值: 连续 {CONSECUTIVE_DUPLICATE_PAGES_TO_STOP} 页无新商品"
    )
    if MAX_SCROLL_ROUNDS > 0:
        print(f"最大滚动轮数: {MAX_SCROLL_ROUNDS}")
    if MAX_PAGES_PER_CATEGORY > 0:
        print(f"最大翻页数: {MAX_PAGES_PER_CATEGORY}")
    print("列表模式: 按页面自动检测（翻页 / 无限下滑）")
    print(f"请求间隔: {REQUEST_DELAY_MS[0]}–{REQUEST_DELAY_MS[1]} ms")
    print(f"评分补全: {ENRICH_MODE}" + (
        f"（并发 {ENRICH_CONCURRENCY}）" if ENRICH_MODE != "off" else ""
    ))
    filter_bits = []
    if LISTING_MIN_PRICE:
        filter_bits.append(f"minPrice={LISTING_MIN_PRICE}")
    if LISTING_MAX_PRICE:
        filter_bits.append(f"maxPrice={LISTING_MAX_PRICE}")
    if LISTING_STAR_FILTER:
        filter_bits.append(f"selectedSwitches=filterCode:{LISTING_STAR_FILTER}")
    if LISTING_SORT_TYPE:
        filter_bits.append(f"SortType={LISTING_SORT_TYPE}")
    print(f"列表过滤: {', '.join(filter_bits) if filter_bits else '关闭'}")
    if QUALITY_FILTER:
        print(
            f"本地质量门: price<{QUALITY_MAX_PRICE} rating≥{QUALITY_MIN_RATING} "
            f"reviews≥{QUALITY_MIN_REVIEWS} sold≥{QUALITY_MIN_SOLD}"
        )
    else:
        print("本地质量门: 关闭")
    print(
        f"类目黑名单: {len(BLOCKED_CATEGORY_TABS)} tabs / "
        f"{len(CATEGORY_BLACKLIST_KEYWORDS)} keywords <- {CATEGORY_BLACKLIST_FILE.name}"
    )
    print()

    seen_links = load_seen_links()
    es_writer = ElasticsearchUrlWriter()

    async with async_playwright() as playwright:
        round_no = 0
        while True:
            round_no += 1
            CURRENT_CRAWL_ROUND = round_no
            if claim_mode and claim_client is not None:
                meta = claim_client.get_round_meta()
                prev = int(meta.get("round") or 0)
                if prev >= round_no:
                    round_no = prev + 1
                    CURRENT_CRAWL_ROUND = round_no
                print(f"\n======== 抓取轮次 #{round_no} 开始 ========")
                # Ensure done/claimed from prior runs can be worked when looping.
                if CRAWL_LOOP or CRAWL_RECLAIM_DONE:
                    # First round: also reclaim stale done so continuous works
                    # even if CRAWL_RECLAIM_DONE=0 but CRAWL_LOOP=1.
                    if round_no > 1 or CRAWL_LOOP:
                        reset_n = await asyncio.to_thread(
                            claim_client.reset_all_pending, crawl_round=round_no
                        )
                        print(f"已重置 {reset_n} 个类目为 pending（round={round_no}）")
                boot_n = await bootstrap_subcategory_seeds(
                    playwright, claim_client, workers=workers
                )
                if boot_n:
                    print(f"子类目 bootstrap 完成：{boot_n} 条写入 ES")

            totals = await run_one_crawl_pass(
                playwright,
                claim_client=claim_client if claim_mode else None,
                categories=categories,
                seen_links=seen_links,
                es_writer=es_writer,
                workers=workers,
                batch_size=batch_size,
            )
            es_writer.flush()

            completed_at = utc_now_iso()
            seed_count = 0
            if claim_mode and claim_client is not None:
                seed_count = await asyncio.to_thread(claim_client.count_enabled_seeds)
                await asyncio.to_thread(
                    claim_client.record_round_complete,
                    round_no=round_no,
                    seed_count=seed_count,
                    product_count=totals.product_count,
                    new_count=totals.new_count,
                    quality_passed=totals.quality_passed,
                )

            print(f"\n—— 轮次 #{round_no} 完成 @ {completed_at} ——")
            print(f"本轮可见/抓取 ID: {totals.product_count}")
            print(f"本轮新增: {totals.new_count}")
            if QUALITY_FILTER:
                print(f"本轮质量门通过(累计写入轮次): {totals.quality_passed}")
            if claim_mode:
                print(f"ES 启用 seeds: {seed_count}")
                print(f"轮次完成时间已写入 ES doc `__crawl_round__`")

            if not CRAWL_LOOP or not claim_mode:
                break
            print(f"循环抓取：休眠 {CRAWL_LOOP_SLEEP_SEC}s 后开始下一轮 …")
            await asyncio.sleep(CRAWL_LOOP_SLEEP_SEC)

    es_writer.close()

    print("\n完成。")
    print(f"并行 worker: {workers}")
    if claim_mode:
        print(f"设备 ID: {device}")
    print(f"累计商品数: {len(seen_links)}")
    print(f"链接文件: {LINKS_FILE}")
    print(f"列表文件: {PRODUCTS_JSONL}")
    if es_writer.enabled:
        print(f"ES 索引: {es_writer.index}")
        print(
            f"ES 本次写入: {es_writer.saved}，失败: {es_writer.failed}，"
            f"跳过未变化: {es_writer.skipped}"
        )


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="AliExpress link crawler")
    parser.add_argument(
        "--discover-categories",
        action="store_true",
        help="Only discover category URLs (L1/lv3) into ES/YAML; skip product crawl",
    )
    parser.add_argument(
        "--site",
        choices=("us", "com"),
        default="us",
        help="Used with --discover-categories (default: us)",
    )
    parser.add_argument(
        "--l1-only",
        action="store_true",
        help="With --discover-categories: only L1 tabs",
    )
    args, _unknown = parser.parse_known_args()
    if args.discover_categories:
        site_base = (
            "https://www.aliexpress.com"
            if args.site == "com"
            else "https://www.aliexpress.us"
        )

        async def _discover() -> None:
            claim_client = build_claim_client()
            async with async_playwright() as playwright:
                await run_category_url_discovery(
                    playwright,
                    site_base=site_base,
                    claim_client=claim_client,
                    include_lv3=not args.l1_only,
                    include_category_hrefs=not args.l1_only,
                    workers=1,
                )

        asyncio.run(_discover())
        return
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
