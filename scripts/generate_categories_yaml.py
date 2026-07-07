#!/usr/bin/env python3
"""Generate config/categories.yaml from AliExpress category CSV snapshots."""

from __future__ import annotations

import csv
import re
from io import StringIO
from pathlib import Path
from urllib.parse import urlsplit

import urllib.request

BASE_DIR = Path(__file__).resolve().parents[1]
OUT_FILE = BASE_DIR / "config" / "categories.yaml"
CSV_BASE = (
    "https://raw.githubusercontent.com/VikasNeha/aliexpress_category_scrape/master/csv"
)

# Keep existing top-level seeds (id -> display name). URLs may redirect; IDs are stable.
EXISTING_L1: dict[str, str] = {
    "34": "Automotive",
    "26": "Toys & Games",
    "66": "Beauty & Health",
    "1509": "Jewelry & Accessories",
    "200001075": "Hair Extensions & Wigs",
    "100006664": "Pet Supplies",
    "509": "Cell Phones & Accessories",
    "44": "Electronics",
    "15": "Patio, Lawn & Garden",
    "13": "Tools & Home Improvement",
    "18": "Sports & Outdoors",
    "17": "Arts, Crafts & Sewing",
    "21": "Office & School Supplies",
}

L1_FROM_CSV: dict[str, str] = {
    "3": "Apparel & Accessories",
    "34": "Automotive",
    "1501": "Baby & Maternity",
    "66": "Beauty & Health",
    "7": "Computer & Office",
    "44": "Consumer Electronics",
    "5": "Electrical Equipment & Supplies",
    "502": "Electronic Components & Supplies",
    "1503": "Furniture",
    "15": "Home & Garden",
    "6": "Home Appliances",
    "13": "Home Improvement",
    "200003590": "Industrial & Business",
    "1509": "Jewelry & Accessories",
    "39": "Lights & Lighting",
    "1524": "Luggage & Bags",
    "21": "Office & School Supplies",
    "509": "Phones & Telecommunications",
    "322": "Shoes",
    "18": "Sports & Entertainment",
    "26": "Toys & Hobbies",
    "1511": "Watches",
    "2": "Food",
    "17": "Gifts & Crafts",
    "200001075": "Hair Extensions & Wigs",
    "100006664": "Pet Supplies",
}

# Prefer yaml-friendly names for categories we already ship.
L1_DISPLAY = {
    "2": "Food",
    "3": "Apparel & Accessories",
    "5": "Electrical Equipment & Supplies",
    "6": "Home Appliances",
    "7": "Computer & Office",
    "13": "Tools & Home Improvement",
    "15": "Patio, Lawn & Garden",
    "17": "Arts, Crafts & Sewing",
    "18": "Sports & Outdoors",
    "21": "Office & School Supplies",
    "26": "Toys & Games",
    "34": "Automotive",
    "39": "Lights & Lighting",
    "44": "Electronics",
    "66": "Beauty & Health",
    "509": "Cell Phones & Accessories",
    "1501": "Baby & Maternity",
    "1509": "Jewelry & Accessories",
    "1511": "Watches",
    "1524": "Luggage & Bags",
    "1503": "Furniture",
    "322": "Shoes",
    "502": "Electronic Components & Supplies",
    "200001075": "Hair Extensions & Wigs",
    "200003590": "Industrial & Business",
    "100006664": "Pet Supplies",
}

# AliExpress top-level categories not present in the public CSV snapshot.
EXTRA_L1: dict[str, tuple[str, str]] = {
    "200001075": "hair-extensions-wigs",
    "100006664": "pet-products",
}

# L2 under these L1 parents (by CSV parent name).
L2_PARENTS = {
    "Apparel & Accessories",
    "Shoes",
    "Baby Products",
    "Computers & Networking",
    "Furniture",
    "Home Appliances",
    "Lights & Lighting",
    "Luggage & Bags",
    "Watches",
    "Food",
    "Industry & Business",
    "Electrical Equipment & Supplies",
    "Electronic Components & Supplies",
}

# L3 under these L2 names (CSV column 2).
L3_PARENTS = {
    "Women",
    "Men",
    "Girls",
    "Boys",
    "Women's Shoes",
    "Men's Shoes",
    "Children's Shoes",
    "Mobile Phones",
    "Laptops",
    "Kitchen Appliances",
    "Weddings & Events",
}


def fetch_csv(name: str) -> list[list[str]]:
    url = f"{CSV_BASE}/{name}"
    with urllib.request.urlopen(url, timeout=60) as resp:
        text = resp.read().decode("utf-8")
    return list(csv.reader(StringIO(text)))


def parse_category_url(url: str) -> tuple[str, str]:
    match = re.search(r"/category/(\d+)/([^/?]+)\.html", url)
    if not match:
        raise ValueError(f"Cannot parse category URL: {url}")
    return match.group(1), match.group(2)


def build_url(site: str, cat_id: str, slug: str) -> str:
    host = "www.aliexpress.us" if site == "US" else "www.aliexpress.com"
    return (
        f"https://{host}/category/{cat_id}/{slug}.html"
        f"?SortType=total_tranpro_desc"
    )


def l1_display_name(cat_id: str, csv_name: str) -> str:
    return L1_DISPLAY.get(cat_id) or EXISTING_L1.get(cat_id) or csv_name


def main() -> None:
    main_rows = fetch_csv("1_main_cats.csv")
    sub_rows = fetch_csv("2_sub_cats.csv")
    sub3_rows = fetch_csv("3_sub_cats.csv")

    l1_by_name: dict[str, tuple[str, str]] = {}
    for row in main_rows:
        name, _, url = row
        cat_id, slug = parse_category_url(url)
        l1_by_name[name] = (cat_id, slug)
    for cat_id, slug in EXTRA_L1.items():
        display = L1_DISPLAY[cat_id]
        l1_by_name[display] = (cat_id, slug)

    entries: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(site: str, name: str, cat_id: str, slug: str) -> None:
        key = (site, cat_id)
        if key in seen:
            return
        seen.add(key)
        entries.append((site, name, build_url(site, cat_id, slug)))

    # Level 1: all main categories, US then COM per category.
    for csv_name, (cat_id, slug) in sorted(l1_by_name.items(), key=lambda x: x[1][0]):
        display = l1_display_name(cat_id, csv_name)
        if cat_id in EXTRA_L1:
            slug = EXTRA_L1[cat_id]
        for site in ("US", "COM"):
            add(site, f"{site} / {display}", cat_id, slug)

    # Level 2: selected high-yield parents.
    for row in sub_rows:
        l2_name, parent_name, url = row
        if parent_name not in L2_PARENTS:
            continue
        cat_id, slug = parse_category_url(url)
        for site in ("US", "COM"):
            parent_display = parent_name
            if parent_name == "Baby Products":
                parent_display = "Baby & Maternity"
            elif parent_name == "Computers & Networking":
                parent_display = "Computer & Office"
            elif parent_name == "Home & Garden":
                parent_display = "Patio, Lawn & Garden"
            elif parent_name == "Toys & Hobbies":
                parent_display = "Toys & Games"
            elif parent_name == "Sports & Entertainment":
                parent_display = "Sports & Outdoors"
            elif parent_name == "Gifts & Crafts":
                parent_display = "Arts, Crafts & Sewing"
            elif parent_name == "Phones & Telecommunications":
                parent_display = "Cell Phones & Accessories"
            elif parent_name == "Consumer Electronics":
                parent_display = "Electronics"
            elif parent_name == "Home Improvement":
                parent_display = "Tools & Home Improvement"
            add(
                site,
                f"{site} / {parent_display} > {l2_name}",
                cat_id,
                slug,
            )

    # Level 3: apparel, shoes, and a few other high-volume branches.
    for row in sub3_rows:
        l3_name, parent_name, url = row
        if parent_name not in L3_PARENTS:
            continue
        cat_id, slug = parse_category_url(url)
        l2_parent = {
            "Women": "Apparel & Accessories",
            "Men": "Apparel & Accessories",
            "Girls": "Apparel & Accessories",
            "Boys": "Apparel & Accessories",
            "Weddings & Events": "Apparel & Accessories",
            "Women's Shoes": "Shoes",
            "Men's Shoes": "Shoes",
            "Children's Shoes": "Shoes",
            "Mobile Phones": "Consumer Electronics",
            "Laptops": "Computers & Networking",
            "Kitchen Appliances": "Home Appliances",
        }[parent_name]
        l1_label = L1_DISPLAY.get(
            l1_by_name[l2_parent][0],
            l2_parent,
        )
        for site in ("US", "COM"):
            add(
                site,
                f"{site} / {l1_label} > {parent_name} > {l3_name}",
                cat_id,
                slug,
            )

    lines = [
        "# AliExpress category URLs (.us + .com)",
        "# Sort by orders to prioritize high-volume products.",
        "#",
        "# Levels:",
        "#   L1  US / Category",
        "#   L2  US / Category > Subcategory",
        "#   L3  US / Category > Subcategory > Leaf",
        "#",
        "# Entries with two ' > ' separators skip auto subcategory discovery (L3 seeds).",
        "# L1/L2 entries still auto-discover deeper categories up to MAX_SUBCATEGORY_DEPTH.",
        "categories:",
    ]
    for site, name, url in entries:
        lines.append(f"  - name: {name}")
        lines.append(f"    url: {url}")
        lines.append("")

    OUT_FILE.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(f"Wrote {len(entries)} entries -> {OUT_FILE}")


if __name__ == "__main__":
    main()
