# -*- coding: utf-8 -*-
"""Multi-device category claim/lease via Elasticsearch crawl_categories index.

Eligible seeds: enabled=true and crawl_status in {missing, pending, failed},
or claimed with expired lease. Atomic scripted update marks claimed_by + lease.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from elasticsearch import Elasticsearch
from elasticsearch.exceptions import ConflictError, NotFoundError


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat()


def default_device_id(explicit: str = "") -> str:
    text = (explicit or "").strip()
    if text:
        return text
    host = socket.gethostname() or "unknown"
    return host


@dataclass
class ClaimedCategory:
    doc_id: str
    name: str
    url: str


@dataclass
class CategoryCrawlStats:
    new_count: int = 0
    product_count: int = 0
    listing_total: int | None = None
    quality_passed: int = 0


ROUND_META_ID = "__crawl_round__"


CLAIM_SCRIPT = """
String status = ctx._source.crawl_status;
boolean available = (status == null || status == '' || status == 'pending' || status == 'failed');
if (!available && status == 'claimed') {
  String exp = ctx._source.claim_expires_at;
  available = (exp == null || exp == '' || exp.compareTo(params.now) < 0);
}
if (!available && params.reclaim_done && status == 'done') {
  available = true;
}
if (!available) {
  ctx.op = 'noop';
} else {
  ctx._source.crawl_status = 'claimed';
  ctx._source.claimed_by = params.device_id;
  ctx._source.claimed_at = params.now;
  ctx._source.claim_expires_at = params.expires;
  ctx._source.updated_at = params.now;
  ctx._source.remove('last_error');
}
"""


class CategoryClaimClient:
    """Claim / heartbeat / complete crawl category seeds in ES."""

    def __init__(
        self,
        es_url: str,
        index: str,
        *,
        device_id: str = "",
        lease_seconds: int = 7200,
        reclaim_done: bool = False,
        enabled_only: bool = True,
    ) -> None:
        self.index = (index or "").strip()
        self.device_id = default_device_id(device_id)
        self.lease_seconds = max(60, int(lease_seconds))
        self.reclaim_done = bool(reclaim_done)
        self.enabled_only = bool(enabled_only)
        placeholder = (
            (not es_url)
            or ("@host:" in es_url)
            or es_url.rstrip("/").endswith("://host:9200")
        )
        self.enabled = bool(self.index and es_url and not placeholder)
        self.client: Elasticsearch | None = None
        if self.enabled:
            self.client = Elasticsearch(
                hosts=[es_url],
                request_timeout=60,
                retry_on_timeout=True,
                max_retries=2,
            )

    def _expires_iso(self, now: datetime | None = None) -> str:
        base = now or utc_now()
        return (base + timedelta(seconds=self.lease_seconds)).isoformat()

    def _eligible_query(self) -> dict[str, Any]:
        must: list[dict[str, Any]] = [
            {"bool": {"must_not": [{"ids": {"values": [ROUND_META_ID]}}]}},
        ]
        if self.enabled_only:
            must.append({"term": {"enabled": True}})

        status_should: list[dict[str, Any]] = [
            {"bool": {"must_not": [{"exists": {"field": "crawl_status"}}]}},
            {"terms": {"crawl_status.keyword": ["pending", "failed"]}},
            {
                "bool": {
                    "must": [{"term": {"crawl_status.keyword": "claimed"}}],
                    "filter": [
                        {
                            "bool": {
                                "should": [
                                    {
                                        "bool": {
                                            "must_not": [
                                                {"exists": {"field": "claim_expires_at"}}
                                            ]
                                        }
                                    },
                                    {"range": {"claim_expires_at": {"lt": "now"}}},
                                ],
                                "minimum_should_match": 1,
                            }
                        }
                    ],
                }
            },
        ]
        if self.reclaim_done:
            status_should.append({"term": {"crawl_status.keyword": "done"}})

        must.append(
            {
                "bool": {
                    "should": status_should,
                    "minimum_should_match": 1,
                }
            }
        )
        return {"bool": {"must": must}}

    def list_candidates(self, size: int = 50) -> list[dict[str, Any]]:
        if not self.enabled or self.client is None:
            return []
        resp = self.client.search(
            index=self.index,
            size=max(1, size),
            query=self._eligible_query(),
            sort=[{"priority": {"order": "asc", "unmapped_type": "long"}}],
            _source=["name", "url", "priority", "crawl_status", "claim_expires_at"],
        )
        rows: list[dict[str, Any]] = []
        for hit in resp.get("hits", {}).get("hits", []):
            src = hit.get("_source") or {}
            name = str(src.get("name") or "").strip()
            url = str(src.get("url") or "").strip()
            if not name or not url:
                continue
            rows.append(
                {
                    "doc_id": hit.get("_id") or name,
                    "name": name,
                    "url": url,
                    "priority": src.get("priority"),
                    "crawl_status": src.get("crawl_status"),
                }
            )
        return rows

    def try_claim(self, doc_id: str) -> ClaimedCategory | None:
        if not self.enabled or self.client is None:
            return None
        now = utc_now()
        now_iso = now.isoformat()
        try:
            resp = self.client.update(
                index=self.index,
                id=doc_id,
                body={
                    "script": {
                        "source": CLAIM_SCRIPT,
                        "lang": "painless",
                        "params": {
                            "device_id": self.device_id,
                            "now": now_iso,
                            "expires": self._expires_iso(now),
                            "reclaim_done": self.reclaim_done,
                        },
                    }
                },
            )
        except NotFoundError:
            return None
        except ConflictError:
            return None

        result = resp.get("result")
        if result == "noop":
            return None
        try:
            got = self.client.get(index=self.index, id=doc_id)
            src = got.get("_source") or {}
        except NotFoundError:
            return None
        name = str(src.get("name") or "").strip()
        url = str(src.get("url") or "").strip()
        if not name or not url:
            return None
        return ClaimedCategory(doc_id=doc_id, name=name, url=url)

    def claim_batch(self, n: int) -> list[ClaimedCategory]:
        """Claim up to n eligible categories for this device."""
        if not self.enabled or self.client is None or n <= 0:
            return []
        claimed: list[ClaimedCategory] = []
        # Over-fetch candidates; races drop some.
        candidates = self.list_candidates(size=max(n * 3, n + 5))
        for row in candidates:
            if len(claimed) >= n:
                break
            item = self.try_claim(row["doc_id"])
            if item is not None:
                claimed.append(item)
        return claimed

    def heartbeat(self, doc_id: str) -> bool:
        if not self.enabled or self.client is None:
            return False
        now = utc_now()
        now_iso = now.isoformat()
        script = """
        if (ctx._source.crawl_status != 'claimed' || ctx._source.claimed_by != params.device_id) {
          ctx.op = 'noop';
        } else {
          ctx._source.claim_expires_at = params.expires;
          ctx._source.updated_at = params.now;
        }
        """
        try:
            resp = self.client.update(
                index=self.index,
                id=doc_id,
                body={
                    "script": {
                        "source": script,
                        "lang": "painless",
                        "params": {
                            "device_id": self.device_id,
                            "now": now_iso,
                            "expires": self._expires_iso(now),
                        },
                    }
                },
            )
        except (NotFoundError, ConflictError):
            return False
        return resp.get("result") != "noop"

    def complete(
        self,
        doc_id: str,
        *,
        product_count: int,
        new_count: int,
        listing_total: int | None = None,
        quality_passed: int | None = None,
        crawl_round: int | None = None,
        crawl_url: str | None = None,
    ) -> None:
        if not self.enabled or self.client is None:
            return
        now_iso = utc_now_iso()
        fields: dict[str, Any] = {
            "crawl_status": "done",
            "claimed_by": self.device_id,
            "last_crawled_at": now_iso,
            "crawled_product_count": int(product_count),
            "crawled_new_count": int(new_count),
            "updated_at": now_iso,
            "claim_expires_at": now_iso,
            "last_error": None,
        }
        if listing_total is not None:
            fields["listing_total"] = int(listing_total)
        if quality_passed is not None:
            fields["crawled_quality_passed"] = int(quality_passed)
        if crawl_round is not None:
            fields["last_crawl_round"] = int(crawl_round)
        if crawl_url:
            fields["last_crawl_url"] = str(crawl_url)
        try:
            self.client.update(index=self.index, id=doc_id, body={"doc": fields})
        except NotFoundError:
            return

    def fail(self, doc_id: str, error: str) -> None:
        if not self.enabled or self.client is None:
            return
        now_iso = utc_now_iso()
        try:
            self.client.update(
                index=self.index,
                id=doc_id,
                body={
                    "doc": {
                        "crawl_status": "failed",
                        "claimed_by": self.device_id,
                        "last_crawled_at": now_iso,
                        "updated_at": now_iso,
                        "claim_expires_at": now_iso,
                        "last_error": (error or "")[:500],
                    }
                },
            )
        except NotFoundError:
            return

    def upsert_seeds(self, docs: list[dict[str, Any]], *, id_field: str = "name") -> int:
        """Partial-update category/subcategory seeds; preserve crawl progress.

        New docs get crawl_status=pending. Existing docs keep claim/crawl fields.
        """
        if not self.enabled or self.client is None or not docs:
            return 0
        ok = 0
        now_iso = utc_now_iso()
        for doc in docs:
            doc_id = str(doc.get(id_field) or "").strip()
            if not doc_id:
                continue
            seed = {k: v for k, v in doc.items() if v is not None}
            seed["updated_at"] = now_iso
            upsert = dict(seed)
            upsert.setdefault("crawl_status", "pending")
            upsert.setdefault("enabled", True)
            try:
                self.client.update(
                    index=self.index,
                    id=doc_id,
                    body={"doc": seed, "upsert": upsert},
                    refresh=False,
                )
                ok += 1
            except Exception:
                continue
        try:
            self.client.indices.refresh(index=self.index)
        except Exception:
            pass
        return ok

    def count_enabled_seeds(self) -> int:
        if not self.enabled or self.client is None:
            return 0
        must: list[dict[str, Any]] = [{"term": {"enabled": True}}]
        must.append({"bool": {"must_not": [{"ids": {"values": [ROUND_META_ID]}}]}})
        try:
            return int(
                self.client.count(index=self.index, query={"bool": {"must": must}})[
                    "count"
                ]
            )
        except Exception:
            return 0

    def get_round_meta(self) -> dict[str, Any]:
        if not self.enabled or self.client is None:
            return {"round": 0}
        try:
            got = self.client.get(index=self.index, id=ROUND_META_ID)
            src = got.get("_source") or {}
            return src if isinstance(src, dict) else {"round": 0}
        except NotFoundError:
            return {"round": 0}
        except Exception:
            return {"round": 0}

    def record_round_complete(
        self,
        *,
        round_no: int,
        seed_count: int,
        product_count: int,
        new_count: int,
        quality_passed: int = 0,
    ) -> None:
        """Persist round completion timestamp + totals (special meta doc)."""
        if not self.enabled or self.client is None:
            return
        now_iso = utc_now_iso()
        doc = {
            "name": ROUND_META_ID,
            "enabled": False,
            "round": int(round_no),
            "last_round_completed_at": now_iso,
            "last_round_seed_count": int(seed_count),
            "last_round_product_count": int(product_count),
            "last_round_new_count": int(new_count),
            "last_round_quality_passed": int(quality_passed),
            "updated_at": now_iso,
            "crawl_status": "meta",
        }
        try:
            self.client.update(
                index=self.index,
                id=ROUND_META_ID,
                body={"doc": doc, "upsert": doc},
                refresh=True,
            )
        except Exception:
            return

    def reset_all_pending(self, *, crawl_round: int | None = None) -> int:
        """Reset enabled seeds to pending for the next continuous crawl round."""
        if not self.enabled or self.client is None:
            return 0
        now_iso = utc_now_iso()
        script = """
        if (ctx._source.name != null && params.name_skip.equals(ctx._source.name)) {
          ctx.op = 'noop';
        } else {
          ctx._source.crawl_status = 'pending';
          ctx._source.remove('claimed_by');
          ctx._source.remove('claimed_at');
          ctx._source.remove('claim_expires_at');
          ctx._source.remove('last_error');
          ctx._source.updated_at = params.now;
          if (params.round != null) {
            ctx._source.next_crawl_round = params.round;
          }
        }
        """
        query: dict[str, Any] = {"term": {"enabled": True}}
        try:
            resp = self.client.update_by_query(
                index=self.index,
                body={
                    "query": query,
                    "script": {
                        "source": script,
                        "lang": "painless",
                        "params": {
                            "now": now_iso,
                            "round": crawl_round,
                            "name_skip": ROUND_META_ID,
                        },
                    },
                },
                conflicts="proceed",
                refresh=True,
            )
            return int(resp.get("updated") or 0)
        except Exception:
            return 0
