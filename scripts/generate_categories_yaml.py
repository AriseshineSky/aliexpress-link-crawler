#!/usr/bin/env python3
"""Generate config/categories.yaml from AliExpress.us homepage calp-plus tabs.

Real navigation (verified 2026-07): homepage category icons point to
  /p/calp-plus/index.html?categoryTab=...
not classic /category/{id}/... pages. Product feeds load via infinite scroll.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import quote

BASE_DIR = Path(__file__).resolve().parents[1]
OUT_FILE = BASE_DIR / "config" / "categories.yaml"

# Fallback if live scrape fails (captured from www.aliexpress.us homepage).
FALLBACK_L1 = [
    "Automotive",
    "Appliances",
    "Women's Clothing",
    "Men's Clothing",
    "Furniture",
    "Toys & Games",
    "Shoes",
    "Beauty & Health",
    "Hair Extensions & Wigs",
    "Jewelry & Accessories",
    "Pet Supplies",
    "Cell Phones & Accessories",
    "Electronics",
    "Patio, Lawn & Garden",
    "Tools & Home Improvement",
    "Bags & Luggage",
    "Novelty & Special Use",
    "Sports & Outdoors",
    "Baby & Maternity",
    "Arts, Crafts & Sewing",
    "Office & School Supplies",
    "Motorcycles & Powersports",
    "Books & Media",
    "Business, Industry & Science",
]


def name_to_category_tab(name: str) -> str:
    return name.strip().lower().replace(" ", "_")


def calp_url(category_tab: str) -> str:
    return (
        "https://www.aliexpress.us/p/calp-plus/index.html"
        f"?categoryTab={quote(category_tab, safe='')}"
    )


async def scrape_home_categories() -> list[dict[str, str]]:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )
        await page.goto("https://www.aliexpress.us/", wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(4000)
        try:
            close = page.locator(".pop-close-btn").first
            if await close.count():
                await close.click(timeout=2000, force=True)
                await page.wait_for_timeout(500)
        except Exception:
            pass

        cats = await page.evaluate(
            """
            () => {
              const out = [];
              const seen = new Set();
              for (const a of document.querySelectorAll('a[href*="categoryTab="]')) {
                const text = (a.innerText || '').trim().replace(/\\s+/g, ' ');
                if (!text || text.length > 60) continue;
                const href = a.getAttribute('href') || '';
                const m = href.match(/categoryTab=([^&]+)/);
                if (!m) continue;
                let tab = decodeURIComponent(m[1]);
                // homepage sometimes double-encodes & and ,
                if (tab.includes('%')) {
                  try { tab = decodeURIComponent(tab); } catch {}
                }
                if (seen.has(tab)) continue;
                seen.add(tab);
                out.push({ name: text, categoryTab: tab });
              }
              return out;
            }
            """
        )
        await browser.close()
        return cats or []


def write_yaml(categories: list[tuple[str, str]]) -> None:
    lines = [
        "# AliExpress US category URLs from homepage calp-plus tabs.",
        "# Product lists load via infinite scroll (not ?page=N).",
        "# Subcategory icons on calp pages are in-page filters (no separate URL);",
        "# the crawler clicks them when CRAWL_SUBCATEGORIES=1.",
        "categories:",
    ]
    for name, url in categories:
        lines.append(f"  - name: {name}")
        lines.append(f"    url: {url}")
        lines.append("")
    OUT_FILE.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def build_entries(l1: list[dict[str, str]]) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for item in l1:
        name = item["name"]
        tab = item.get("categoryTab") or name_to_category_tab(name)
        entries.append((f"US / {name}", calp_url(tab)))
    return entries


def main() -> None:
    l1: list[dict[str, str]] = []
    try:
        l1 = asyncio.run(scrape_home_categories())
    except Exception as exc:
        print(f"Live homepage scrape failed ({exc}); using fallback list.")

    if len(l1) < 10:
        l1 = [{"name": n, "categoryTab": name_to_category_tab(n)} for n in FALLBACK_L1]
        print(f"Using fallback L1 list ({len(l1)} categories).")
    else:
        print(f"Scraped {len(l1)} L1 categories from homepage.")

    entries = build_entries(l1)
    write_yaml(entries)
    print(f"Wrote {len(entries)} entries -> {OUT_FILE}")


if __name__ == "__main__":
    main()
