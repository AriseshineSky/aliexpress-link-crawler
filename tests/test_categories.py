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

    def test_build_page_url(self) -> None:
        from alilj import build_page_url

        base = (
            "https://www.aliexpress.us/w/wholesale-testing-and-inspection.html"
            "?maxPrice=99&SortType=total_tranpro_desc"
        )
        self.assertEqual(build_page_url(base, 1), base)
        page2 = build_page_url(base, 2)
        self.assertIn("page=2", page2)
        self.assertIn("maxPrice=99", page2)
        cat = "https://www.aliexpress.us/category/34/auto.html?SortType=total_tranpro_desc"
        cat2 = build_page_url(cat, 3)
        self.assertIn("page=3", cat2)
        self.assertIn("CatId=34", cat2)

    def test_infer_listing_load_mode(self) -> None:
        from alilj import infer_listing_load_mode

        self.assertEqual(
            infer_listing_load_mode(has_pagination_dom=True, url="https://x/calp-plus"),
            "pagination",
        )
        self.assertEqual(
            infer_listing_load_mode(
                has_pagination_dom=False,
                url="https://www.aliexpress.us/w/wholesale-lip.html",
            ),
            "pagination",
        )
        self.assertEqual(
            infer_listing_load_mode(
                has_pagination_dom=False,
                url="https://www.aliexpress.us/p/calp-plus/index.html?categoryTab=auto",
            ),
            "scroll",
        )
        self.assertEqual(
            infer_listing_load_mode(
                has_pagination_dom=False,
                url="https://www.aliexpress.us/category/34/auto.html",
            ),
            "pagination",
        )

    def test_with_listing_filters_price_and_stars(self) -> None:
        url = (
            "https://www.aliexpress.us/category/0/Car-Gadgets-%26-Appliances.html"
            "?isFromCategory=y&postCatIds=200000287%2C200003425&g=y"
        )
        filtered = with_listing_filters(url)
        self.assertIn("maxPrice=99", filtered)
        self.assertIn("selectedSwitches=filterCode%3A4StarRating", filtered)
        self.assertIn("SortType=total_tranpro_desc", filtered)
        self.assertIn("postCatIds=200000287", filtered)

    def test_wholesale_search_url_includes_filters(self) -> None:
        url = wholesale_search_url("US / Automotive", "https://www.aliexpress.us")
        self.assertIn("SortType=total_tranpro_desc", url)
        self.assertIn("maxPrice=99", url)
        self.assertIn("selectedSwitches=filterCode%3A4StarRating", url)

    def test_calp_url_gets_sort_type(self) -> None:
        url = with_listing_filters(
            "https://www.aliexpress.us/p/calp-plus/index.html?categoryTab=automotive"
        )
        self.assertIn("SortType=total_tranpro_desc", url)
        self.assertIn("categoryTab=automotive", url)

    def test_product_passes_quality(self) -> None:
        from alilj import ListingProduct, product_passes_quality

        good = ListingProduct(
            product_id="1",
            url="https://www.aliexpress.us/item/1.html",
            source="aliexpress.us",
            price=50.0,
            rating=4.5,
            reviews=400,
            sold_count=600,
        )
        self.assertTrue(product_passes_quality(good))
        bad = ListingProduct(
            product_id="2",
            url="https://www.aliexpress.us/item/2.html",
            source="aliexpress.us",
            price=50.0,
            rating=4.5,
            reviews=10,
            sold_count=600,
        )
        self.assertFalse(product_passes_quality(bad))

    def test_save_writes_even_when_quality_fails(self) -> None:
        from alilj import ListingProduct, save_new_products
        from pathlib import Path
        import tempfile

        bad = ListingProduct(
            product_id="99",
            url="https://www.aliexpress.us/item/99.html",
            source="aliexpress.us",
            price=50.0,
            rating=4.5,
            reviews=1,
            sold_count=1,
        )
        seen: set[str] = set()
        old_links = alilj.LINKS_FILE
        old_jsonl = alilj.PRODUCTS_JSONL
        try:
            with tempfile.TemporaryDirectory() as tmp:
                alilj.LINKS_FILE = Path(tmp) / "links.txt"
                alilj.PRODUCTS_JSONL = Path(tmp) / "products.jsonl"
                new_count, quality_n = save_new_products([bad], seen, "US / Test")
                self.assertEqual(new_count, 1)
                self.assertEqual(quality_n, 0)
                self.assertTrue(alilj.LINKS_FILE.exists())
                self.assertIn("99", alilj.LINKS_FILE.read_text(encoding="utf-8"))
        finally:
            alilj.LINKS_FILE = old_links
            alilj.PRODUCTS_JSONL = old_jsonl

    def test_build_subcategory_seed_docs(self) -> None:
        from alilj import build_subcategory_seed_docs

        docs = build_subcategory_seed_docs(
            "US / Beauty & Health",
            "https://www.aliexpress.us/p/calp-plus/index.html?categoryTab=beauty_%26_health",
            ["Face Makeup", "Nail Art"],
            parent_priority=3,
        )
        self.assertEqual(len(docs), 2)
        self.assertEqual(docs[0]["name"], "US / Beauty & Health > Face Makeup")
        self.assertIn("/w/wholesale-", docs[0]["url"])
        self.assertIn("SortType=total_tranpro_desc", docs[0]["url"])
        self.assertIn("calpLv3=", docs[0]["calp_url"])
        self.assertEqual(docs[0]["parent_name"], "US / Beauty & Health")
        self.assertEqual(docs[0]["priority"], 3001)

    def test_category_tab_display_name(self) -> None:
        from alilj import category_tab_display_name

        self.assertEqual(category_tab_display_name("automotive"), "Automotive")
        self.assertEqual(
            category_tab_display_name("toys_%26_games"),
            "Toys & Games",
        )

    def test_build_l1_seed_docs_filters_blacklist(self) -> None:
        from alilj import build_calp_l1_url, build_l1_seed_docs

        tabs = [
            {
                "name": "Automotive",
                "tab": "automotive",
                "url": build_calp_l1_url("https://www.aliexpress.us", "automotive"),
            },
            {
                "name": "Women's Clothing",
                "tab": "women's_clothing",
                "url": build_calp_l1_url(
                    "https://www.aliexpress.us", "women's_clothing"
                ),
            },
        ]
        docs = build_l1_seed_docs(tabs)
        names = [d["name"] for d in docs]
        self.assertIn("US / Automotive", names)
        self.assertTrue(all("Clothing" not in n for n in names))
        self.assertEqual(docs[0]["seed_type"], "calp_l1")
        self.assertIn("categoryTab=automotive", docs[0]["url"])

    def test_write_discovered_category_files(self) -> None:
        import tempfile

        from alilj import write_discovered_category_files

        docs = [
            {
                "name": "US / Automotive",
                "url": "https://www.aliexpress.us/p/calp-plus/index.html?categoryTab=automotive",
                "seed_type": "calp_l1",
            },
            {
                "name": "US / Automotive > Gauges",
                "url": "https://www.aliexpress.us/w/wholesale-gauges.html",
                "seed_type": "calp_lv3_wholesale",
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            yaml_path = Path(tmp) / "cats.yaml"
            jsonl_path = Path(tmp) / "cats.jsonl"
            write_discovered_category_files(
                docs, yaml_path=yaml_path, jsonl_path=jsonl_path
            )
            text = yaml_path.read_text(encoding="utf-8")
            self.assertIn("US / Automotive", text)
            self.assertNotIn("Gauges", text)
            lines = jsonl_path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 2)

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
