#!/usr/bin/env python3
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from elasticsearch import Elasticsearch, helpers

load_dotenv(Path(__file__).resolve().parent / ".env")
url = os.getenv("ELASTICSEARCH_URL")
index = os.getenv("ELASTICSEARCH_INDEX_URLS")
SOURCE = "aliexpress.us"
now = datetime.now(timezone.utc).isoformat()
links_file = Path(__file__).resolve().parent / "产品链接.txt"
actions = []
for line in links_file.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    match = re.search(r"/item/(\d+)\.html", line)
    if not match:
        continue
    product_id = match.group(1)
    actions.append(
        {
            "_index": index,
            "_id": f"{SOURCE}_{product_id}",
            "_op_type": "index",
            "_source": {
                "source": SOURCE,
                "product_id": product_id,
                "url": line,
                "category": "Automotive",
                "scraped_at": now,
                "created_at": now,
                "updated_at": now,
            },
        }
    )
client = Elasticsearch(hosts=[url], timeout=60)
ok, errs = helpers.bulk(client, actions, raise_on_error=False)
failed = len(errs) if isinstance(errs, list) else 0
print(f"backfill ok={ok} failed={failed}")
