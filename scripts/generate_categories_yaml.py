#!/usr/bin/env python3
"""Generate config/categories.yaml from the priority US calp-plus seed list.

Historical note: homepage has many more L1 tabs (including Women's/Men's Clothing).
This project intentionally crawls only the priority set in FALLBACK_L1 / categories.priority.yaml.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

BASE_DIR = Path(__file__).resolve().parents[1]
OUT_FILE = BASE_DIR / "config" / "categories.yaml"
PRIORITY_FILE = BASE_DIR / "config" / "categories.priority.yaml"

# Keep aligned with ES enabled crawl categories / categories.priority.yaml.
FALLBACK_L1 = [
    "Automotive",
    "Toys & Games",
    "Beauty & Health",
    "Jewelry & Accessories",
    "Hair Extensions & Wigs",
    "Pet Supplies",
    "Cell Phones & Accessories",
    "Electronics",
    "Patio, Lawn & Garden",
    "Tools & Home Improvement",
    "Sports & Outdoors",
    "Arts, Crafts & Sewing",
    "Office & School Supplies",
]


def name_to_category_tab(name: str) -> str:
    return name.strip().lower().replace(" ", "_")


def calp_url(category_tab: str) -> str:
    return (
        "https://www.aliexpress.us/p/calp-plus/index.html"
        f"?categoryTab={quote(category_tab, safe='')}"
    )


def write_yaml(names: list[str]) -> None:
    lines = [
        "# AliExpress US priority crawl seeds (calp-plus).",
        "# Keep in sync with config/categories.priority.yaml / ES enabled categories.",
        "# Product lists load via infinite scroll (not ?page=N).",
        "# Subcategory icons on calp pages are in-page filters (no separate URL);",
        "# the crawler clicks them when CRAWL_SUBCATEGORIES=1.",
        "categories:",
    ]
    for name in names:
        tab = name_to_category_tab(name)
        lines.append(f"  - name: US / {name}")
        lines.append(f"    url: {calp_url(tab)}")
        lines.append("")
    OUT_FILE.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(f"Wrote {len(names)} categories -> {OUT_FILE}")


def main() -> None:
    import yaml

    if PRIORITY_FILE.exists():
        raw = yaml.safe_load(PRIORITY_FILE.read_text(encoding="utf-8")) or {}
        names = []
        for item in raw.get("categories") or []:
            name = str(item.get("name") or "")
            if name.startswith("US / "):
                names.append(name.split(" / ", 1)[1])
            elif name:
                names.append(name)
        if names:
            write_yaml(names)
            return
    write_yaml(FALLBACK_L1)


if __name__ == "__main__":
    main()
