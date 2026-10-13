#!/usr/bin/env python3
"""Collect the PDFs published on the JSN (Japanese Society of Neurology) "Publications / Guidelines" page into cache/jns_guidelines/pdf/.

The source material is the set of PDFs under the society site <https://www.neurology-jp.org/guidelinem/index.html>
(190 files / about 6,600 pages: 189 OK in download_log + 1 re-fetched). The fetch record is
`,/JNS_Guidelines_ALL/download_log.csv` left by the first scrape (columns: Guideline, PDF, Status, URL, Filename, Message).

This script has two roles:

  1. **Rebuild the catalog** (`--rebuild-catalog`)
     download_log.csv → `scripts/jns_guidelines_catalog.csv` (tracked in git).
     The build / manifest side only looks at this file and never depends on the work logs under `,`.
     - Status=DUPLICATE means "the same URL was already saved under another name", so drop it (one real file)
     - Status=DOWNLOAD_ERROR is a single row (the HAM2025 main body). It was **re-fetched by hand the same day**,
       so keep the URL and leave it as an OK row
     - One row has a filename longer than 255 bytes (epilepsy 2018 supplement CQ4-6).
       It hit the ext4 filename limit and is **not in the local zip**. In the catalog,
       `file` is the name truncated to 250 bytes and `local` keeps the original name
  2. **Place into cache** — the default is a network fetch (for later re-fetching); `--from-local DIR`
     copies from a local PDF directory (use this for the first run). Both store under the name in the `file` column.

usage:
    uv run python scripts/fetch_jns_guidelines.py --rebuild-catalog
    uv run python scripts/fetch_jns_guidelines.py --from-local ../,/JNS_Guidelines_ALL
    uv run --group ingest python scripts/fetch_jns_guidelines.py --limit 2   # smoke test of the network fetch
  (run from inside cage/. Only the network fetch needs requests = --group ingest)
"""

from __future__ import annotations

import argparse
import csv
import shutil
import sys
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlparse

CATALOG = Path("scripts/jns_guidelines_catalog.csv")
PDF_DIR = Path("cache/jns_guidelines/pdf")
DEFAULT_LOG = Path("../,/JNS_Guidelines_ALL/download_log.csv")

UA = "llens-cage/0.1 (LLENS開発チーム <<redacted-email>>)"
SLEEP = 2.0  # scrape rule in docs/cage/01_brief.md (2 seconds or more)

# Rescue for the row still left as DOWNLOAD_ERROR in download_log.
# (The fetch itself was completed by hand on 2026-09-05; the URL in the log is correct)
ERROR_RESCUE = {
    "https://www.neurology-jp.org/guidelinem/pdf/ham_2025_01.pdf": (
        "HTLV-1関連脊髄症（HAM）診療ガイドライン2025_PDF.pdf"
    ),
}

# Truncation width with some margin under the ext4 filename limit (255 bytes).
# The link text on the society page is used as-is for the filename, so exactly one exceeds it
NAME_MAX_BYTES = 250


def cache_name(name: str) -> str:
    """Truncate a filename to 250 bytes or less (without splitting a character)."""
    name = unicodedata.normalize("NFC", name)
    if len(name.encode()) <= NAME_MAX_BYTES:
        return name
    stem, dot, ext = name.rpartition(".")
    stem = stem or name
    budget = NAME_MAX_BYTES - len(f"{dot}{ext}".encode())
    out: list[str] = []
    used = 0
    for ch in stem:
        w = len(ch.encode())
        if used + w > budget:
            break
        out.append(ch)
        used += w
    return f"{''.join(out)}{dot}{ext}"


def url_slug(url: str) -> str:
    """Basename of the URL (all ASCII and unique on the society site). Base for the served PDF name and overlay name."""
    return Path(urlparse(url).path).name


def rebuild_catalog(log: Path) -> None:
    if not log.exists():
        sys.exit(f"download_log not found: {log} (specify its location with --log)")
    rows = list(csv.DictReader(log.open(encoding="utf-8-sig")))
    out: list[dict[str, str]] = []
    seen_url: set[str] = set()
    n_dup = n_err = 0
    for r in rows:
        url = r["URL"].strip()
        status = r["Status"].strip()
        if status == "DUPLICATE":
            n_dup += 1
            continue
        if status == "DOWNLOAD_ERROR":
            if url not in ERROR_RESCUE:
                print(f"  ! unrescued DOWNLOAD_ERROR: {url} ({r['Message'][:60]})", file=sys.stderr)
                n_err += 1
                continue
            r = dict(r, Filename=ERROR_RESCUE[url])
        if url in seen_url:
            n_dup += 1
            continue
        seen_url.add(url)
        orig = unicodedata.normalize("NFC", r["Filename"].strip())
        name = cache_name(orig)
        out.append(
            {
                "file": name,
                "slug": url_slug(url),
                "url": url,
                "guideline": r["Guideline"].strip(),
                "pdf": r["PDF"].strip(),
                "local": "" if name == orig else orig,
            }
        )

    slugs = [r["slug"] for r in out]
    if len(set(slugs)) != len(slugs):
        sys.exit("URL basenames collide (served PDF names would not be unique)")

    CATALOG.parent.mkdir(parents=True, exist_ok=True)
    with CATALOG.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["file", "slug", "url", "guideline", "pdf", "local"])
        w.writeheader()
        w.writerows(out)
    print(f"catalog={CATALOG} rows={len(out)} dropped_dup={n_dup} unrescued_error={n_err}")


def load_catalog() -> list[dict[str, str]]:
    if not CATALOG.exists():
        sys.exit(f"catalog not found: {CATALOG} (create it with --rebuild-catalog)")
    return list(csv.DictReader(CATALOG.open(encoding="utf-8")))


def copy_local(rows: list[dict[str, str]], src: Path, force: bool) -> None:
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    n_ok = n_skip = n_miss = 0
    for r in rows:
        dst = PDF_DIR / r["file"]
        if dst.exists() and not force:
            n_skip += 1
            continue
        # The local zip uses the download_log names as-is, so also look for the pre-truncation name
        cand = [src / r["file"]] + ([src / r["local"]] if r["local"] else [])
        hit = next((c for c in cand if c.exists()), None)
        if hit is None:
            print(f"  ! not found locally: {r['file']}  ({r['url']})", file=sys.stderr)
            n_miss += 1
            continue
        shutil.copyfile(hit, dst)
        n_ok += 1
    print(f"copied={n_ok} skipped={n_skip} missing={n_miss} → {PDF_DIR}")


def download(rows: list[dict[str, str]], force: bool, dry_run: bool) -> None:
    n_ok = n_skip = n_err = 0
    session = None
    for r in rows:
        dst = PDF_DIR / r["file"]
        if dst.exists() and not force:
            n_skip += 1
            continue
        if dry_run:
            print(f"GET {r['url']} → {dst}")
            n_ok += 1
            continue
        if session is None:
            try:
                import requests  # --group ingest
            except ImportError:
                sys.exit("requests is required: uv run --group ingest python scripts/fetch_jns_guidelines.py")
            session = requests.Session()
            session.headers["User-Agent"] = UA
            PDF_DIR.mkdir(parents=True, exist_ok=True)
        try:
            resp = session.get(r["url"], timeout=120)
            resp.raise_for_status()
            if not resp.content.startswith(b"%PDF"):
                raise ValueError(f"response is not a PDF ({len(resp.content)} bytes)")
        except Exception as e:  # noqa: BLE001 — one failure must not stop the whole run
            print(f"  ! fetch failed: {r['url']} — {e}", file=sys.stderr)
            n_err += 1
            time.sleep(SLEEP)
            continue
        dst.write_bytes(resp.content)
        print(f"  {dst.name} ({len(resp.content)} bytes)")
        n_ok += 1
        time.sleep(SLEEP)
    print(f"fetched={n_ok} skipped={n_skip} failed={n_err} → {PDF_DIR}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rebuild-catalog", action="store_true", help="rebuild the catalog from download_log.csv")
    ap.add_argument("--log", type=Path, default=DEFAULT_LOG, help="location of download_log.csv")
    ap.add_argument("--from-local", type=Path, metavar="DIR", help="copy from a local directory instead of the network")
    ap.add_argument("--limit", type=int, help="process only the first N files (for smoke tests)")
    ap.add_argument("--force", action="store_true", help="re-fetch even if already in cache")
    ap.add_argument("--dry-run", action="store_true", help="only print the URLs that would be fetched")
    args = ap.parse_args()

    if args.rebuild_catalog:
        rebuild_catalog(args.log)
        if args.from_local is None and not args.dry_run and args.limit is None:
            return

    rows = load_catalog()
    if args.limit:
        rows = rows[: args.limit]
    if args.from_local:
        copy_local(rows, args.from_local, args.force)
    else:
        download(rows, args.force, args.dry_run)


if __name__ == "__main__":
    main()
