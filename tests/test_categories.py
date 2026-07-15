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

import alilj  # noqa: E402
from alilj import (  # noqa: E402
    ElasticsearchUrlWriter,
    ListingProduct,
    category_path_depth,
    is_blacklisted_category,
    is_blocked_category_url,
    is_calp_url,
    is_login_url,
    load_categories,
    wholesale_search_url,
    with_listing_filters,
    worker_user_data_dir,
    worker_window_bounds,
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

    def test_worker_user_data_dir(self) -> None:
        old = alilj.CRAWL_WORKERS
        try:
            alilj.CRAWL_WORKERS = 1
            self.assertEqual(worker_user_data_dir(0), alilj.USER_DATA_DIR)
            alilj.CRAWL_WORKERS = 3
            self.assertEqual(worker_user_data_dir(0), alilj.USER_DATA_DIR)
            self.assertEqual(
                worker_user_data_dir(1),
                alilj.USER_DATA_DIR / "worker-1",
            )
        finally:
            alilj.CRAWL_WORKERS = old

    def test_worker_window_bounds_tiles_four(self) -> None:
        os.environ["SCREEN_WIDTH"] = "1920"
        os.environ["SCREEN_HEIGHT"] = "1080"
        try:
            boxes = [worker_window_bounds(i, 4) for i in range(4)]
            self.assertEqual(boxes[0], (0, 0, 960, 540))
            self.assertEqual(boxes[1], (960, 0, 960, 540))
            self.assertEqual(boxes[2], (0, 540, 960, 540))
            self.assertEqual(boxes[3], (960, 540, 960, 540))
        finally:
            os.environ.pop("SCREEN_WIDTH", None)
            os.environ.pop("SCREEN_HEIGHT", None)

    def test_is_login_url(self) -> None:
        self.assertTrue(is_login_url("https://login.aliexpress.com/?return_url=x"))
        self.assertTrue(is_login_url("https://www.aliexpress.us/p/account/login"))
        self.assertFalse(is_login_url("https://www.aliexpress.us/p/calp-plus/index.html"))

    def test_category_path_depth(self) -> None:
        self.assertEqual(category_path_depth("US / Shoes"), 1)
        self.assertEqual(category_path_depth("US / Apparel & Accessories > Women"), 2)
        self.assertEqual(
            category_path_depth("US / Apparel & Accessories > Women > Dresses"),
            3,
        )

    def test_load_categories_count(self) -> None:
        categories = load_categories()
        self.assertGreaterEqual(len(categories), 13)

    def test_category_urls_are_us_calp_plus(self) -> None:
        for name, url in load_categories():
            self.assertTrue(name.startswith("US / "), msg=f"Bad site prefix: {name}")
            self.assertRegex(url, CATEGORY_URL_RE, msg=f"Bad URL for {name}")
            self.assertTrue(is_calp_url(url))
            self.assertNotIn("aliexpress.com/", url)
            self.assertFalse(is_blocked_category_url(url), msg=name)

    def test_required_top_level_categories(self) -> None:
        names = {
            name.split(" / ", 1)[1]
            for name, _ in load_categories()
            if " > " not in name
        }
        required = {
            "Automotive",
            "Toys & Games",
            "Beauty & Health",
            "Pet Supplies",
            "Hair Extensions & Wigs",
            "Electronics",
            "Office & School Supplies",
        }
        missing = required - names
        self.assertFalse(missing, msg=f"Missing L1 categories: {missing}")
        self.assertNotIn("Women's Clothing", names)
        self.assertNotIn("Men's Clothing", names)

    def test_blocks_clothing_category_urls(self) -> None:
        self.assertTrue(
            is_blocked_category_url(
                "https://www.aliexpress.us/p/calp-plus/index.html"
                "?categoryTab=women%27s_clothing&maxPrice=99"
            )
        )
        self.assertTrue(
            is_blocked_category_url(
                "https://www.aliexpress.us/p/calp-plus/index.html"
                "?categoryTab=men's_clothing&selectedSwitches=filterCode%3A4StarRating"
            )
        )
        self.assertFalse(
            is_blocked_category_url(
                "https://www.aliexpress.us/p/calp-plus/index.html?categoryTab=automotive"
            )
        )

    def test_clothing_blacklist_keywords(self) -> None:
        self.assertTrue(is_blacklisted_category(name="US / Women's Clothing"))
        self.assertTrue(is_blacklisted_category(name="Women's Dresses"))
        self.assertTrue(is_blacklisted_category(name="Men's Jackets"))
        self.assertTrue(is_blacklisted_category(name="Athletic Clothing"))
        self.assertFalse(is_blacklisted_category(name="US / Automotive"))
        self.assertFalse(is_blacklisted_category(name="Car Electronics"))
        self.assertFalse(is_blacklisted_category(name="Pet Supplies"))

    def test_adult_sensitive_blacklist(self) -> None:
        self.assertTrue(
            is_blacklisted_category(name="US / Novelty & Special Use")
        )
        self.assertTrue(
            is_blocked_category_url(
                "https://www.aliexpress.us/p/calp-plus/index.html"
                "?categoryTab=novelty_%26_special_use"
            )
        )
        self.assertTrue(is_blacklisted_category(name="Adult Sex Toys"))
        self.assertTrue(is_blacklisted_category(name="Erotic Products"))
        self.assertTrue(is_blacklisted_category(name="成人用品"))
        self.assertFalse(is_blacklisted_category(name="US / Toys & Games"))
        self.assertFalse(is_blacklisted_category(name="Office & School Supplies"))

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
        self.assertEqual(len(raw["categories"]), 13)
        self.assertTrue(all(c["url"].startswith("https://www.aliexpress.us/") for c in raw["categories"]))
        self.assertTrue(all("calp-plus" in c["url"] for c in raw["categories"]))
        tabs = " ".join(c["url"] for c in raw["categories"]).lower()
        self.assertNotIn("women", tabs)
        self.assertNotIn("men%27s_clothing", tabs)
        self.assertNotIn("men's_clothing", tabs)


class ElasticsearchDedupeTests(unittest.TestCase):
    def test_skips_unchanged_fingerprint(self) -> None:
        writer = ElasticsearchUrlWriter.__new__(ElasticsearchUrlWriter)
        writer.enabled = True
        writer.client = object()  # truthy sentinel; write exits before network if skipped
        writer.index = "test-index"
        writer.chunk_size = 1000
        writer.buffer = []
        writer.saved = 0
        writer.failed = 0
        writer.skipped = 0
        writer._lock = __import__("threading").Lock()
        writer._written_fp = {}

        product = ListingProduct(
            product_id="123",
            url="https://www.aliexpress.us/item/123.html",
            source="aliexpress.us",
            title="Widget",
            price=9.9,
            rating=4.8,
            reviews=100,
            sold_count=50,
        )
        writer.write(product, "US / Toys")
        self.assertEqual(len(writer.buffer), 1)
        self.assertEqual(writer.skipped, 0)

        writer.write(product, "US / Toys")
        self.assertEqual(len(writer.buffer), 1)
        self.assertEqual(writer.skipped, 1)

        richer = ListingProduct(
            product_id="123",
            url=product.url,
            source=product.source,
            title=product.title,
            price=product.price,
            rating=4.9,
            reviews=120,
            sold_count=50,
        )
        writer.write(richer, "US / Toys")
        self.assertEqual(len(writer.buffer), 2)
        self.assertEqual(writer.skipped, 1)


if __name__ == "__main__":
    unittest.main()
