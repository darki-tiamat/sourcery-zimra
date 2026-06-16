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
import re
import sys
import time
from urllib.parse import urljoin, urlparse, unquote, parse_qs

import requests
from bs4 import BeautifulSoup
import pdfplumber

BASE_URL = "https://www.zimra.co.zw"

# Comprehensive seed pages covering all ZIMRA download categories.
# The crawler follows pagination (?start=N) on each page automatically.
SEED_PAGES = [
    # Legislation / Acts
    "https://www.zimra.co.zw/downloads/category/17-acts",
    # Annual Reports
    "https://www.zimra.co.zw/downloads/category/2-annual-reports",
    # Domestic Taxes (PAYE, VAT, corporate, forms, etc.)
    "https://www.zimra.co.zw/downloads/category/9-domestic-taxes",
    # Public Notices
    "https://www.zimra.co.zw/public-notices",
    # Revenue Performance Reports
    "https://www.zimra.co.zw/downloads/category/12-revenue-perfomance-reports",
    # Exchange Rates (current)
    "https://www.zimra.co.zw/downloads/category/10-exchange-rates",
    # Exchange Rates 2021
    "https://www.zimra.co.zw/downloads/category/41-exchange-rates-2021",
    # Exchange Rates 2022
    "https://www.zimra.co.zw/downloads/category/75-exchange-rates-2022",
    # Exchange Rates 2023+
    "https://www.zimra.co.zw/downloads/category/77-exchange-rates-2023",
    # Consumer Price Index
    "https://www.zimra.co.zw/downloads/category/37-consumer-price-index",
    # Customs rulings
    "https://www.zimra.co.zw/customs/rulings",
    # Customs documents
    "https://www.zimra.co.zw/customs/customs-documents",
    # Rummage Auction Sales
    "https://www.zimra.co.zw/rummage-auction-sales",
    # Client Satisfaction Surveys
    "https://www.zimra.co.zw/client-satisfaction-surveys",
    # Vacancies
    "https://www.zimra.co.zw/vacancies",
    # AEO (Authorised Economic Operator)
    "https://www.zimra.co.zw/authorised-economic-operator-aeo",
    # Strategic Plans
    "https://www.zimra.co.zw/about-us/strategic-plan",
    "https://www.zimra.co.zw/downloads/category/57-strategic-plans",
    # TaRMS FAQs
    "https://www.zimra.co.zw/frequently-asked-questions/tarms-faqs",
    # Anti-Money Laundering
    "https://www.zimra.co.zw/revenue-assurance-and-special-project/anti-money-laundering",
    # Audited Financial Results
    "https://www.zimra.co.zw/about-us/audited-financial-results",
    # Transfer Pricing Documentation
    "https://www.zimra.co.zw/downloads/category/44-transfer-pricing-documentation",
    # Publications
    "https://www.zimra.co.zw/public-notices/publications",
    # Domestic Taxes sub-pages with PDFs
    "https://www.zimra.co.zw/domestic-taxes/individual/pay-as-you-earn-paye",
    "https://www.zimra.co.zw/domestic-taxes/corporate/tax-rates",
    "https://www.zimra.co.zw/domestic-taxes/tax-tables",
    "https://www.zimra.co.zw/domestic-taxes/vat/mechanics-of-vat",
    # Customs sub-pages with PDFs
    "https://www.zimra.co.zw/customs/customs-and-excise-duties",
    "https://www.zimra.co.zw/customs/customs-clearance-procedures",
    "https://www.zimra.co.zw/customs/commercial-guidelines-on-imports-exports",
    "https://www.zimra.co.zw/customs/classification-of-goods/tariff-handbook",
    "https://www.zimra.co.zw/customs/classification-of-goods/customs-and-excise-duties-2022",
    "https://www.zimra.co.zw/customs/what-is-excise-duty-and-surtax",
    "https://www.zimra.co.zw/customs/carbon-tax-rates-road-access-fees",
    # Legislation main page
    "https://www.zimra.co.zw/legislation",
    # Exchange of Information
    "https://www.zimra.co.zw/exchange-of-information-eoi",
]

OUTPUT_FILE = "index.ndjson"
USER_AGENT = "SourceryCrawler/1.0 (+https://github.com/darkian-corpuses)"
REQUEST_TIMEOUT = 60
MIN_CHUNK_CHARS = 40
PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT})

def discover_pdf_urls():
    """Return a de-duplicated, ordered list of absolute PDF URLs.

    For each seed page, scrape PDF anchor links and follow pagination
    links (?start=N) to discover all documents across multi-page listings.
    """
    found = []
    seen = set()
    visited_pages = set()

    def _scrape_page(url):
        """Scrape a single page for PDF links, return list of pagination URLs."""
        if url in visited_pages:
            return []
        visited_pages.add(url)
        pagination_urls = []
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as exc:
            print(f"[warn] page failed {url}: {exc}", file=sys.stderr)
            return []
        soup = BeautifulSoup(resp.text, "html.parser")
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"]
            absolute = urljoin(url, href)
            parsed = urlparse(absolute)
            path = parsed.path.lower()
            qs = parse_qs(parsed.query)
            # Collect PDF links (direct .pdf files)
            if path.endswith(".pdf") and absolute not in seen:
                seen.add(absolute)
                found.append(absolute)
            # Collect download links (/downloads?download=ID:slug) as PDFs to fetch
            elif "download" in qs and absolute not in seen:
                seen.add(absolute)
                found.append(absolute)
            # Detect pagination links (?start=N) on the same domain
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

    return found

def title_from_url(url):
    """Derive a human-readable title from a URL."""
    parsed = urlparse(url)
    # Handle /downloads?download=ID:slug format
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
    # ZIMRA download links may redirect; update title source if needed
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

def main():
    urls = discover_pdf_urls()
    print(f"[info] discovered {len(urls)} PDF URLs", file=sys.stderr)

    written = 0
    with open(OUTPUT_FILE, "w", encoding="utf-8") as out:
        for url in urls:
            try:
                data, final_url = fetch_pdf_bytes(url)
            except requests.RequestException as exc:
                print(f"[warn] download failed {url}: {exc}", file=sys.stderr)
                continue
            title = title_from_url(final_url)
            try:
                for page_number, chunk in chunks_from_pdf(data):
                    record = {
                        "url": url,
                        "title": title,
                        "page": page_number,
                        "chunk": chunk,
                    }
                    out.write(json.dumps(record, ensure_ascii=False) + "\n")
                    written += 1
            except Exception as exc:  # pdfplumber can raise on malformed PDFs
                print(f"[warn] extract failed {url}: {exc}", file=sys.stderr)
            finally:
                del data  # discard bytes; never persisted
            time.sleep(1)

    print(f"[info] wrote {written} chunks to {OUTPUT_FILE}", file=sys.stderr)

if __name__ == "__main__":
    main()
