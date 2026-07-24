#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Smoke-test listing mode detection + short crawl for calp (scroll) and wholesale (pagination)."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

# Keep the smoke run short.
os.environ.setdefault("MAX_SCROLL_ROUNDS", "2")
os.environ.setdefault("MAX_PAGES_PER_CATEGORY", "2")
os.environ.setdefault("CRAWL_WORKERS", "1")
os.environ.setdefault("QUALITY_FILTER", "0")
os.environ.setdefault("ENRICH_MODE", "off")
os.environ.setdefault("SUBCATEGORY_AS_SEEDS", "0")
os.environ.setdefault("CRAWL_SUBCATEGORIES", "0")

from playwright.async_api import async_playwright

import alilj
from alilj import (
    apply_orders_sort_ui,
    close_browser,
    create_browser,
    crawl_listing,
    detect_listing_load_mode,
    dismiss_popups,
    handle_captcha,
    safe_goto,
    site_base_from_url,
    with_listing_filters,
)

CASES = [
    {
        "label": "calp-scroll",
        "expect": "scroll",
        "name": "US / Automotive (smoke)",
        "url": with_listing_filters(
            "https://www.aliexpress.us/p/calp-plus/index.html?categoryTab=automotive"
        ),
    },
    {
        "label": "wholesale-pagination",
        "expect": "pagination",
        "name": "US / Automotive wholesale (smoke)",
        # Avoid maxPrice so the listing has multiple pages for page=2 verification.
        "url": (
            "https://www.aliexpress.us/w/wholesale-automotive.html"
            "?SortType=total_tranpro_desc&sortType=total_tranpro_desc"
            "&selectedSwitches=filterCode%3A4StarRating"
        ),
    },
]


async def run_case(playwright, case: dict) -> dict:
    print("\n" + "=" * 60)
    print(f"CASE {case['label']}")
    print(f"URL: {case['url']}")
    print("=" * 60)
    context, page, collector = await create_browser(playwright, 0, workers=1)
    result = {
        "label": case["label"],
        "expect": case["expect"],
        "mode": None,
        "ok": False,
        "product_count": 0,
        "new_count": 0,
        "error": None,
    }
    try:
        await safe_goto(page, case["url"], worker_id=0)
        await dismiss_popups(page)
        await handle_captcha(page, worker_id=0)
        await apply_orders_sort_ui(page, worker_id=0)
        mode = await detect_listing_load_mode(page)
        result["mode"] = mode
        print(f"detected mode={mode} (expect={case['expect']})")
        if mode != case["expect"]:
            result["error"] = f"mode mismatch: got {mode}, expect {case['expect']}"
            print(f"FAIL: {result['error']}")
            return result

        seen: set[str] = set()
        stats = await crawl_listing(
            page,
            case["name"],
            site_base_from_url(case["url"]),
            seen,
            collector,
            None,
            worker_id=0,
            listing_url=case["url"],
        )
        result["product_count"] = stats.product_count
        result["new_count"] = stats.new_count
        result["ok"] = stats.product_count > 0
        print(
            f"crawl done: products={stats.product_count} new={stats.new_count} "
            f"listing_total={stats.listing_total}"
        )
        if not result["ok"]:
            result["error"] = "no products extracted"
            print("FAIL: no products")
        else:
            print("PASS")
        return result
    except Exception as exc:
        result["error"] = str(exc)
        print(f"FAIL: {exc}")
        return result
    finally:
        await close_browser(context)


async def main() -> int:
    # Force short limits even if .env overrides earlier imports.
    alilj.MAX_SCROLL_ROUNDS = 2
    alilj.MAX_PAGES_PER_CATEGORY = 2
    alilj.CRAWL_WORKERS = 1
    alilj.QUALITY_FILTER = False
    alilj.ENRICH_MODE = "off"
    alilj.CRAWL_SUBCATEGORIES = False
    alilj.SUBCATEGORY_AS_SEEDS = False

    print("Smoke test: scroll vs pagination")
    print(f"MAX_SCROLL_ROUNDS={alilj.MAX_SCROLL_ROUNDS}")
    print(f"MAX_PAGES_PER_CATEGORY={alilj.MAX_PAGES_PER_CATEGORY}")

    results: list[dict] = []
    async with async_playwright() as playwright:
        for case in CASES:
            results.append(await run_case(playwright, case))

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    failed = 0
    for r in results:
        status = "PASS" if r["ok"] else "FAIL"
        print(
            f"[{status}] {r['label']}: mode={r['mode']} "
            f"products={r['product_count']} err={r['error']}"
        )
        if not r["ok"]:
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
