#!/usr/bin/env python3
"""
crawl_aws_docs.py  AWS docs crawler using requests + BeautifulSoup.
Uses sitemaps for URL discovery and lastmod for incremental updates.

Usage:
    python scripts/crawl_aws_docs.py           # full crawl
    python scripts/crawl_aws_docs.py --update  # only changed pages
"""

import argparse
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup

SITEMAPS = [
    "https://docs.aws.amazon.com/AmazonS3/latest/userguide/sitemap.xml",
    "https://docs.aws.amazon.com/AmazonECS/latest/developerguide/sitemap.xml",
    "https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/sitemap.xml",
    "https://docs.aws.amazon.com/lambda/latest/dg/sitemap.xml",
    "https://docs.aws.amazon.com/IAM/latest/UserGuide/sitemap.xml",
    "https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/sitemap.xml",
    "https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/sitemap.xml",
    "https://docs.aws.amazon.com/vpc/latest/userguide/sitemap.xml",
    "https://docs.aws.amazon.com/cdk/v2/guide/sitemap.xml",
]

# Always resolve paths relative to project root (parent of scripts/)
PROJECT_ROOT  = Path(__file__).resolve().parent.parent
OUT_PATH      = PROJECT_ROOT / "data" / "corpus.jsonl"
LAST_RUN_PATH = PROJECT_ROOT / "data" / "last_run.json"
MAX_WORKERS   = 10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def load_last_run() -> dict:
    if LAST_RUN_PATH.exists():
        return json.loads(LAST_RUN_PATH.read_text())
    return {}


def save_last_run():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    data = {s: now for s in SITEMAPS}
    LAST_RUN_PATH.write_text(json.dumps(data, indent=2))


def load_existing_corpus() -> dict:
    corpus = {}
    if OUT_PATH.exists():
        with open(OUT_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    doc = json.loads(line)
                    corpus[doc["id"]] = doc
    return corpus


def save_corpus(corpus: dict):
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        for doc in corpus.values():
            f.write(json.dumps(doc, ensure_ascii=False) + "\n")


def get_sitemap_entries(sitemap_url: str) -> list[dict]:
    """Parse sitemap XML → list of {url, lastmod}"""
    try:
        resp = requests.get(sitemap_url, headers=HEADERS, timeout=10)
        if resp.status_code != 200:
            print(f"  ✗ sitemap failed ({resp.status_code}): {sitemap_url}")
            return []
        root = ElementTree.fromstring(resp.content)
        ns   = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        entries = []
        for url_el in root.findall(".//sm:url", ns):
            loc     = url_el.findtext("sm:loc", namespaces=ns)
            lastmod = url_el.findtext("sm:lastmod", namespaces=ns) or ""
            if loc:
                entries.append({"url": loc, "lastmod": lastmod})
        return entries
    except Exception as e:
        print(f"  ✗ sitemap error: {e}")
        return []


def fetch_page(url: str) -> dict | None:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=12)
        if resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, "html.parser")

        h1 = soup.find("h1")
        if not h1:
            return None
        title = clean(h1.get_text())

        content = (
            soup.find("div", {"id": "main-content"}) or
            soup.find("div", {"class": "awsdocs-container"}) or
            soup.find("article") or
            soup.find("main")
        )
        if not content:
            return None

        for tag in content.find_all(["nav", "footer", "header", "script", "style"]):
            tag.decompose()

        text = clean(content.get_text())
        if len(text) < 150:
            return None

        doc_id  = url.rstrip("/").split("/")[-1].replace(".html", "")
        return {
            "id":      doc_id,
            "title":   title,
            "text":    text,
            "snippet": text[:350],
            "url":     url,
        }
    except Exception:
        return None


def crawl(update_mode: bool):
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    last_run = load_last_run()
    corpus   = load_existing_corpus() if update_mode else {}

    print(f"\n  Mode    : {'incremental update' if update_mode else 'full crawl'}")
    print(f"  Existing: {len(corpus)} docs\n")

    all_urls   = []
    skip_count = 0

    for sitemap_url in SITEMAPS:
        service = sitemap_url.split("/")[4]
        print(f"[{service}] reading sitemap...")
        entries     = get_sitemap_entries(sitemap_url)
        last_run_ts = last_run.get(sitemap_url, "")

        for entry in entries:
            if update_mode and last_run_ts and entry["lastmod"] and entry["lastmod"] <= last_run_ts:
                skip_count += 1
                continue
            all_urls.append(entry["url"])

        print(f"  → {len(entries)} total, {len(all_urls)} to fetch")

    print(f"\nFetching {len(all_urls)} pages with {MAX_WORKERS} workers...\n")

    new_count = 0
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    write_mode = "a" if update_mode else "w"

    with open(OUT_PATH, write_mode, encoding="utf-8") as out_file, \
         ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:

        futures = {executor.submit(fetch_page, url): url for url in all_urls}
        for future in as_completed(futures):
            doc = future.result()
            if doc:
                corpus[doc["id"]] = doc
                out_file.write(json.dumps(doc, ensure_ascii=False) + "\n")
                out_file.flush()
                new_count += 1
                print(f"  [{new_count:04d}] {doc['title'][:70]}")

    save_last_run()

    print(f"\n✓ Done.")
    print(f"  Fetched : {new_count}")
    print(f"  Skipped : {skip_count} (unchanged)")
    print(f"  Total   : {len(corpus)} docs")
    print(f"  Saved   : {OUT_PATH}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--update", action="store_true",
                        help="Only fetch pages changed since last run")
    args = parser.parse_args()
    crawl(update_mode=args.update)
