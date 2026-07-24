#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Discover AliExpress category URLs before product crawl.

Opens calp-plus, scrapes L1 categoryTab links, then each L1 for lv3 chips
(wholesale seeds) and classic /category/{id}/ links. Upserts into
ELASTICSEARCH_INDEX_CATEGORIES and writes local YAML/JSONL.

Usage:
  .venv/bin/python discover_categories.py
  .venv/bin/python discover_categories.py --site us --l1-only
  .venv/bin/python discover_categories.py --site com --no-es
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from playwright.async_api import async_playwright

from alilj import build_claim_client, run_category_url_discovery


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Crawl AliExpress category URLs (L1 + subcategories) into ES/YAML."
    )
    parser.add_argument(
        "--site",
        choices=("us", "com"),
        default=(
            "com"
            if (os.getenv("DISCOVER_SITE") or "").strip().lower() == "com"
            else "us"
        ),
        help="Target site host (default: us, or DISCOVER_SITE=com)",
    )
    parser.add_argument(
        "--l1-only",
        action="store_true",
        help="Only discover L1 categoryTab seeds (skip lv3 and /category/ links)",
    )
    parser.add_argument(
        "--no-lv3",
        action="store_true",
        help="Skip calp lv3 → wholesale seed discovery",
    )
    parser.add_argument(
        "--no-category-hrefs",
        action="store_true",
        help="Skip classic /category/{id}/ link discovery",
    )
    parser.add_argument(
        "--no-es",
        action="store_true",
        help="Do not upsert to Elasticsearch (local files only)",
    )
    parser.add_argument(
        "--yaml-out",
        type=Path,
        default=None,
        help="L1 YAML path (default: config/categories.discovered.yaml)",
    )
    parser.add_argument(
        "--jsonl-out",
        type=Path,
        default=None,
        help="All seeds JSONL path (default: data/category_urls.jsonl)",
    )
    return parser.parse_args(argv)


async def main_async(args: argparse.Namespace) -> int:
    site_base = (
        "https://www.aliexpress.com"
        if args.site == "com"
        else "https://www.aliexpress.us"
    )
    include_lv3 = not args.l1_only and not args.no_lv3
    include_hrefs = not args.l1_only and not args.no_category_hrefs
    claim_client = None if args.no_es else build_claim_client()

    async with async_playwright() as playwright:
        await run_category_url_discovery(
            playwright,
            site_base=site_base,
            claim_client=claim_client,
            include_lv3=include_lv3,
            include_category_hrefs=include_hrefs,
            yaml_out=args.yaml_out,
            jsonl_out=args.jsonl_out,
            workers=1,
        )
    return 0


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    raise SystemExit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main(sys.argv[1:])
