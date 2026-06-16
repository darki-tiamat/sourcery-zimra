#!/usr/bin/env python3
"""Sourcery corpus crawler (ZIMRA).

Discovers PDF URLs by scraping anchor tags on the configured seed pages,
downloads each PDF into memory, extracts text per page with pdfplumber,
splits each page into paragraph chunks, and writes one NDJSON line per chunk
to index.ndjson. PDF bytes are never written to disk.

NDJSON line shape:
  {"url": "...", "title": "...", "page": 1, "chunk": "..."}
"""

import io
import json
import logging
import re
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse, unquote, parse_qs

import requests
from bs4 import BeautifulSoup
import pdfplumber
from tqdm import tqdm

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stderr,
)
log = logging.getLogger("zimra-crawler")

BASE_URL = "https://www.zimra.co.zw"

SEED_PAGES = [
    "https://www.zimra.co.zw/downloads/category/17-acts",
    "https://www.zimra.co.zw/downloads/category/2-annual-reports",
    "https://www.zimra.co.zw/downloads/category/9-domestic-taxes",
    "https://www.zimra.co.zw/public-notices",
    "https://www.zimra.co.zw/downloads/category/12-revenue-perfomance-reports",
    "https://www.zimra.co.zw/downloads/category/10-exchange-rates",
    "https://www.zimra.co.zw/downloads/category/41-exchange-rates-2021",
    "https://www.zimra.co.zw/downloads/category/75-exchange-rates-2022",
    "https://www.zimra.co.zw/downloads/category/77-exchange-rates-2023",
    "https://www.zimra.co.zw/downloads/category/37-consumer-price-index",
    "https://www.zimra.co.zw/customs/rulings",
    "https://www.zimra.co.zw/customs/customs-documents",
    "https://www.zimra.co.zw/rummage-auction-sales",
    "https://www.zimra.co.zw/client-satisfaction-surveys",
    "https://www.zimra.co.zw/vacancies",
    "https://www.zimra.co.zw/authorised-economic-operator-aeo",
    "https://www.zimra.co.zw/about-us/strategic-plan",
    "https://www.zimra.co.zw/downloads/category/57-strategic-plans",
    "https://www.zimra.co.zw/frequently-asked-questions/tarms-faqs",
    "https://www.zimra.co.zw/revenue-assurance-and-special-project/anti-money-laundering",
    "https://www.zimra.co.zw/about-us/audited-financial-results",
    "https://www.zimra.co.zw/downloads/category/44-transfer-pricing-documentation",
    "https://www.zimra.co.zw/public-notices/publications",
    "https://www.zimra.co.zw/domestic-taxes/individual/pay-as-you-earn-paye",
    "https://www.zimra.co.zw/domestic-taxes/corporate/tax-rates",
    "https://www.zimra.co.zw/domestic-taxes/tax-tables",
    "https://www.zimra.co.zw/domestic-taxes/vat/mechanics-of-vat",
    "https://www.zimra.co.zw/customs/customs-and-excise-duties",
    "https://www.zimra.co.zw/customs/customs-clearance-procedures",
    "https://www.zimra.co.zw/customs/commercial-guidelines-on-imports-exports",
    "https://www.zimra.co.zw/customs/classification-of-goods/tariff-handbook",
    "https://www.zimra.co.zw/customs/classification-of-goods/customs-and-excise-duties-2022",
    "https://www.zimra.co.zw/customs/what-is-excise-duty-and-surtax",
    "https://www.zimra.co.zw/customs/carbon-tax-rates-road-access-fees",
    "https://www.zimra.co.zw/legislation",
    "https://www.zimra.co.zw/exchange-of-information-eoi",
]

OUTPUT_FILE = "index.ndjson"
USER_AGENT = "SourceryCrawler/1.0 (+https://github.com/darkian-corpuses)"
REQUEST_TIMEOUT = 60
MIN_CHUNK_CHARS = 40
PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")
MAX_WORKERS = 16
PAGE_DELAY = 0.3  # delay between seed page fetches

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT})

# Thread-safe output
_write_lock = threading.Lock()
_written_count = 0
_count_lock = threading.Lock()


def discover_pdf_urls():
    """Return a de-duplicated, ordered list of absolute PDF URLs."""
    found = []
    seen = set()
    visited_pages = set()

    def _scrape_page(url):
        if url in visited_pages:
            return []
        visited_pages.add(url)
        pagination_urls = []
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as exc:
            log.warning("page failed %s: %s", url, exc)
            return []
        soup = BeautifulSoup(resp.text, "lxml")
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"]
            absolute = urljoin(url, href)
            parsed = urlparse(absolute)
            path = parsed.path.lower()
            qs = parse_qs(parsed.query)
            if path.endswith(".pdf") and absolute not in seen:
                seen.add(absolute)
                found.append(absolute)
            elif "download" in qs and absolute not in seen:
                seen.add(absolute)
                found.append(absolute)
            if "start" in qs and parsed.netloc == urlparse(BASE_URL).netloc:
                if absolute not in visited_pages:
                    pagination_urls.append(absolute)
        return pagination_urls

    for page in SEED_PAGES:
        queue = [page]
        while queue:
            current = queue.pop(0)
            next_pages = _scrape_page(current)
            queue.extend(next_pages)
        time.sleep(PAGE_DELAY)

    return found


def title_from_url(url):
    """Derive a human-readable title from a URL."""
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    if "download" in qs:
        slug = qs["download"][0].split(":", 1)[-1] if ":" in qs["download"][0] else qs["download"][0]
        name = unquote(slug)
    else:
        name = unquote(parsed.path.rsplit("/", 1)[-1])
    if name.lower().endswith(".pdf"):
        name = name[:-4]
    return name.replace("_", " ").replace("-", " ").strip() or url


def fetch_pdf_bytes(url):
    """Download PDF bytes, following redirects to the actual file."""
    resp = session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
    resp.raise_for_status()
    return resp.content, resp.url


def chunks_from_pdf(data):
    """Yield (page_number, chunk_text) for paragraph chunks in the PDF."""
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page_index, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            for raw in PARAGRAPH_SPLIT.split(text):
                chunk = " ".join(raw.split())
                if len(chunk) >= MIN_CHUNK_CHARS:
                    yield page_index, chunk


def process_one_pdf(url, pbar):
    """Download and extract a single PDF. Returns number of chunks written."""
    global _written_count
    title = title_from_url(url)
    pdf_start = time.monotonic()
    try:
        data, final_url = fetch_pdf_bytes(url)
    except requests.RequestException as exc:
        log.warning("download failed [%s]: %s", title, exc)
        pbar.update(1)
        return 0

    try:
        title = title_from_url(final_url)
        chunks = list(chunks_from_pdf(data))
        elapsed = time.monotonic() - pdf_start
        log.info("extracted [%s] %d chunks in %.1fs", title, len(chunks), elapsed)
    except Exception as exc:
        elapsed = time.monotonic() - pdf_start
        log.warning("extract failed [%s] after %.1fs: %s", title, elapsed, exc)
        pbar.update(1)
        return 0
    finally:
        del data

    records = []
    for page_number, chunk in chunks:
        records.append(json.dumps({
            "url": url,
            "title": title,
            "page": page_number,
            "chunk": chunk,
        }, ensure_ascii=False))

    with _write_lock:
        with open(OUTPUT_FILE, "a", encoding="utf-8") as out:
            out.write("\n".join(records) + "\n")
        _written_count += len(records)

    pbar.update(1)
    return len(records)


def main():
    global _written_count

    t0 = time.monotonic()
    urls = discover_pdf_urls()
    discovery_time = time.monotonic() - t0
    total = len(urls)
    log.info("discovered %d PDF URLs in %.1fs", total, discovery_time)

    if total == 0:
        log.info("no PDFs found, exiting")
        return

    # Clear output file
    with open(OUTPUT_FILE, "w", encoding="utf-8") as out:
        pass

    written = 0
    with tqdm(total=total, desc="Extracting PDFs", unit="pdf", ncols=80) as pbar:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {pool.submit(process_one_pdf, url, pbar): url for url in urls}
            for future in as_completed(futures):
                try:
                    written += future.result()
                except Exception as exc:
                    url = futures[future]
                    log.error("unexpected error [%s]: %s", url, exc)

    elapsed = time.monotonic() - t0
    rate = _written_count / elapsed if elapsed > 0 else 0
    log.info(
        "done: %d chunks from %d PDFs in %.1fs (%.1f chunks/s)",
        _written_count, total, elapsed, rate,
    )


if __name__ == "__main__":
    main()
