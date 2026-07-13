# -*- coding: utf-8 -*-
from __future__ import annotations

import re
import subprocess
import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from alilj import category_path_depth, is_calp_url, load_categories  # noqa: E402

CATEGORY_URL_RE = re.compile(
    r"^https://www\.aliexpress\.us/p/calp-plus/index\.html\?categoryTab=.+$"
)


class CategoryConfigTests(unittest.TestCase):
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
