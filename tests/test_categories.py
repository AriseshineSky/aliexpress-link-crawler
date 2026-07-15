# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from alilj import (  # noqa: E402
    category_path_depth,
    is_calp_url,
    load_categories,
    wholesale_search_url,
    with_listing_filters,
)

CATEGORY_URL_RE = re.compile(
    r"^https://www\.aliexpress\.us/p/calp-plus/index\.html\?categoryTab=.+$"
)


class CategoryConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        # Prefer YAML fixtures for unit tests (ignore live ES seeds).
        self._old_es_cats = os.environ.pop("ELASTICSEARCH_INDEX_CATEGORIES", None)

    def tearDown(self) -> None:
        if self._old_es_cats is not None:
            os.environ["ELASTICSEARCH_INDEX_CATEGORIES"] = self._old_es_cats

    def test_with_listing_filters_price_and_stars(self) -> None:
        url = (
            "https://www.aliexpress.us/category/0/Car-Gadgets-%26-Appliances.html"
            "?isFromCategory=y&postCatIds=200000287%2C200003425&g=y"
        )
        filtered = with_listing_filters(url)
        self.assertIn("maxPrice=99", filtered)
        self.assertIn("selectedSwitches=filterCode%3A4StarRating", filtered)
        self.assertIn("postCatIds=200000287", filtered)

    def test_wholesale_search_url_includes_filters(self) -> None:
        url = wholesale_search_url("US / Automotive", "https://www.aliexpress.us")
        self.assertIn("SortType=total_tranpro_desc", url)
        self.assertIn("maxPrice=99", url)
        self.assertIn("selectedSwitches=filterCode%3A4StarRating", url)

    def test_category_path_depth(self) -> None:
        self.assertEqual(category_path_depth("US / Shoes"), 1)
        self.assertEqual(category_path_depth("US / Apparel & Accessories > Women"), 2)
        self.assertEqual(
            category_path_depth("US / Apparel & Accessories > Women > Dresses"),
            3,
        )

    def test_load_categories_count(self) -> None:
        categories = load_categories()
        self.assertGreaterEqual(len(categories), 20)

    def test_category_urls_are_us_calp_plus(self) -> None:
        for name, url in load_categories():
            self.assertTrue(name.startswith("US / "), msg=f"Bad site prefix: {name}")
            self.assertRegex(url, CATEGORY_URL_RE, msg=f"Bad URL for {name}")
            self.assertTrue(is_calp_url(url))
            self.assertNotIn("aliexpress.com/", url)

    def test_required_top_level_categories(self) -> None:
        names = {
            name.split(" / ", 1)[1]
            for name, _ in load_categories()
            if " > " not in name
        }
        required = {
            "Automotive",
            "Women's Clothing",
            "Men's Clothing",
            "Shoes",
            "Pet Supplies",
            "Hair Extensions & Wigs",
            "Electronics",
            "Baby & Maternity",
        }
        missing = required - names
        self.assertFalse(missing, msg=f"Missing L1 categories: {missing}")

    def test_generate_categories_script(self) -> None:
        script = ROOT / "scripts" / "generate_categories_yaml.py"
        result = subprocess.run(
            [sys.executable, str(script)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)

        raw = yaml.safe_load((ROOT / "config" / "categories.yaml").read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(raw["categories"]), 20)
        self.assertTrue(all(c["url"].startswith("https://www.aliexpress.us/") for c in raw["categories"]))
        self.assertTrue(all("calp-plus" in c["url"] for c in raw["categories"]))


if __name__ == "__main__":
    unittest.main()
