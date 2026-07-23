# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from category_claim import (
    CLAIM_SCRIPT,
    CategoryClaimClient,
    CategoryCrawlStats,
    ClaimedCategory,
    default_device_id,
)


class CategoryClaimUnitTests(unittest.TestCase):
    def test_default_device_id_explicit(self) -> None:
        self.assertEqual(default_device_id("pc-a"), "pc-a")

    def test_claim_batch_empty_when_disabled(self) -> None:
        client = CategoryClaimClient("", "", device_id="x")
        self.assertFalse(client.enabled)
        self.assertEqual(client.claim_batch(2), [])

    def test_try_claim_success(self) -> None:
        client = CategoryClaimClient(
            "http://user:pass@localhost:9200",
            "cats",
            device_id="pc-a",
            lease_seconds=100,
        )
        es = MagicMock()
        client.client = es
        client.enabled = True
        es.update.return_value = {"result": "updated"}
        es.get.return_value = {
            "_source": {
                "name": "US / Automotive",
                "url": "https://www.aliexpress.us/p/calp-plus/index.html?categoryTab=automotive",
            }
        }
        claimed = client.try_claim("US / Automotive")
        self.assertIsInstance(claimed, ClaimedCategory)
        assert claimed is not None
        self.assertEqual(claimed.name, "US / Automotive")
        body = es.update.call_args.kwargs.get("body") or es.update.call_args[1].get("body")
        if body is None:
            body = es.update.call_args[0][0] if False else es.update.call_args[1]["body"]
        # elasticsearch-py 7: body= kw
        call_kwargs = es.update.call_args.kwargs or {}
        if not call_kwargs:
            # positional/index style
            call_kwargs = {
                "index": es.update.call_args[1].get("index"),
                "body": es.update.call_args[1].get("body"),
            }
        script = (call_kwargs.get("body") or es.update.call_args[1]["body"])["script"]
        self.assertIn("crawl_status", CLAIM_SCRIPT)
        self.assertEqual(script["params"]["device_id"], "pc-a")

    def test_try_claim_noop(self) -> None:
        client = CategoryClaimClient(
            "http://user:pass@localhost:9200",
            "cats",
            device_id="pc-a",
        )
        es = MagicMock()
        client.client = es
        client.enabled = True
        es.update.return_value = {"result": "noop"}
        self.assertIsNone(client.try_claim("US / Automotive"))

    def test_complete_writes_counts(self) -> None:
        client = CategoryClaimClient(
            "http://user:pass@localhost:9200",
            "cats",
            device_id="pc-a",
        )
        es = MagicMock()
        client.client = es
        client.enabled = True
        client.complete(
            "US / Automotive",
            product_count=120,
            new_count=40,
            listing_total=5000,
            quality_passed=33,
            crawl_round=2,
            crawl_url="https://example/x",
        )
        body = es.update.call_args[1]["body"]
        doc = body["doc"]
        self.assertEqual(doc["crawl_status"], "done")
        self.assertEqual(doc["crawled_product_count"], 120)
        self.assertEqual(doc["crawled_new_count"], 40)
        self.assertEqual(doc["listing_total"], 5000)
        self.assertEqual(doc["crawled_quality_passed"], 33)
        self.assertEqual(doc["last_crawl_round"], 2)

    def test_stats_defaults(self) -> None:
        stats = CategoryCrawlStats()
        self.assertEqual(stats.new_count, 0)
        self.assertEqual(stats.product_count, 0)
        self.assertEqual(stats.quality_passed, 0)
        self.assertIsNone(stats.listing_total)

    def test_upsert_seeds(self) -> None:
        client = CategoryClaimClient(
            "http://user:pass@localhost:9200",
            "cats",
            device_id="pc-a",
        )
        es = MagicMock()
        client.client = es
        client.enabled = True
        n = client.upsert_seeds(
            [
                {
                    "name": "US / Beauty > Lip",
                    "url": "https://www.aliexpress.us/w/wholesale-lip.html",
                    "enabled": True,
                }
            ]
        )
        self.assertEqual(n, 1)
        self.assertTrue(es.update.called)

    def test_record_round_complete(self) -> None:
        client = CategoryClaimClient(
            "http://user:pass@localhost:9200",
            "cats",
            device_id="pc-a",
        )
        es = MagicMock()
        client.client = es
        client.enabled = True
        client.record_round_complete(
            round_no=3,
            seed_count=50,
            product_count=1000,
            new_count=20,
            quality_passed=15,
        )
        args = es.update.call_args
        self.assertEqual(args.kwargs.get("id") or args[1].get("id"), "__crawl_round__")
        body = args.kwargs.get("body") or args[1]["body"]
        doc = body["doc"]
        self.assertEqual(doc["round"], 3)
        self.assertIn("last_round_completed_at", doc)


class ListingTotalExtractTests(unittest.TestCase):
    def test_extract_from_payload(self) -> None:
        import alilj

        total = alilj._extract_listing_total_from_payload(
            {"data": {"pageInfo": {"totalCount": 12345}, "productsV2": []}}
        )
        self.assertEqual(total, 12345)

    def test_collector_keeps_max_total(self) -> None:
        import alilj

        collector = alilj.ListingCollector()
        collector.note_listing_total(100)
        collector.note_listing_total(50)
        collector.note_listing_total(200)
        self.assertEqual(collector.listing_total, 200)


if __name__ == "__main__":
    unittest.main()
