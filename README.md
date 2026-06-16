# Sourcery Corpus Crawler

This folder is a **template** for turning a GitHub repo into a Sourcery corpus.
Each corpus is one repo that publishes a single `index.ndjson` at its root.

## How it works

1. `crawl.py` scrapes the `SEED_PAGES` for PDF anchor links.
2. Each PDF is downloaded **into memory** (never written to disk) and text is
   extracted per page with `pdfplumber`.
3. Each page is split into paragraph chunks; one NDJSON line is written per
   chunk to `index.ndjson`.
4. A scheduled GitHub Action commits `index.ndjson`. The commit also resets the
   60-day Actions inactivity timer, keeping the cron alive.

## NDJSON line format

    {"url": "https://.../doc.pdf", "title": "Doc Title", "page": 3, "chunk": "..."}

Required: `url` (string), `page` (number), `chunk` (string). Optional: `title`.

## Set up a new corpus repo

1. Create a new GitHub repo for the corpus.
2. Copy `crawl.py` and `requirements.txt` into a `crawler/` folder in that repo.
3. Copy `workflow.yml` to `.github/workflows/crawl.yml`.
4. Edit `SEED_PAGES` (and `BASE_URL`) in `crawl.py` to target the source site.
5. Run the workflow once via **Actions → crawl → Run workflow** to publish the
   first `index.ndjson`.

## Register in the app

- First-party: add a `Corpus` entry to `lib/corpus/first_party_corpuses.dart`
  with the raw index URL:
  `https://raw.githubusercontent.com/<org>/<repo>/main/index.ndjson`
- Third-party / self-hosted: paste the raw NDJSON URL in the app's
  **Corpuses → Add custom index URL**.

## Local run

    pip install -r requirements.txt
    python crawl.py
    # produces index.ndjson in the working directory
