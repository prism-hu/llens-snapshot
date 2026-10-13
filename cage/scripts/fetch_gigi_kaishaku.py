#!/usr/bin/env python3
"""Fetch the gigi kaishaku (Q&A interpretation notices from the MHLW Health Insurance Bureau, Medical Economics Division) for the FY2026 (Reiwa 8) medical fee revision into cache/gigi_kaishaku/.

The source is the "３．疑義解釈・訂正通知" (Q&A / corrections) section of the revision special page
<https://www.mhlw.go.jp/stf/newpage_67729.html>. Same page as `fetch_shinryo_hoshu.py`, but that one
fetches the notifications / circulars (and only records the Q&A link list in its manifest), so the
Q&A PDFs are fetched here. Re-fetching the medical fee schedule and re-fetching the Q&A happen at
**different frequencies** (a corrected notification about once every few months, a Q&A every 2-3 weeks),
so the script and the cache are kept separate.

Output (cache/gigi_kaishaku/):
  - mhlw_kaitei.html        ... raw HTML of the special page (evidence of link resolution)
  - sono01.pdf / .txt       ... "疑義解釈資料の送付について (その1)" (Q&A No. 1)  * txt is pdftotext -layout
  - sono02_teisei01.pdf/.txt... partial correction of "その2" (a correction is tied to its original "その N")
  - manifest.json           ... issue number / issue date / URL / bytes / sha256 / fetch date

What is picked and what is not:

  - **picked**: "疑義解釈資料の送付について（その N）" and "『疑義解釈資料の送付について
    （その N）』の一部訂正について". Both are circulars from the Medical Economics Division, and the
    label carries the issue date (`（令和８年４月１日保険局医療課事務連絡）`)
  - **not picked**: "療養の給付と直接関係ないサービス等の取扱いに関する疑義解釈資料"
    (not a Q&A on the fee schedule but on non-covered charges) and "改定関連通知及び官報掲載事項の
    一部訂正について" (errata of the notifications themselves, not a Q&A).
    Only their URLs are kept under `excluded` in the manifest

Pitfalls hit:

  - "その N" in the label uses **full-width digits when N is 1 digit, half-width when 2 digits** (`（その１）` / `（その10）`).
    Same quirk as the notification headings (`第10部`). Collapse with NFKC before parsing
  - The issue date also mixes full-width digits (`令和８年４月１日`). NFKC likewise
  - The special page has **no Q&A from earlier fiscal years** (FY2024 is on another page). No risk of
    mixing up years, but conversely **the special page URL changes with each revision** (`newpage_67729`
    is the FY2026 one). Replace `KAITEI_URL` at the next revision

usage:
    uv run --group ingest python scripts/fetch_gigi_kaishaku.py            # fetch everything
    uv run --group ingest python scripts/fetch_gigi_kaishaku.py --dry-run  # only list the links
  (run from inside cage/)
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html as htmllib
import json
import re
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

import requests

CACHE = Path("cache/gigi_kaishaku")

KAITEI_URL = "https://www.mhlw.go.jp/stf/newpage_67729.html"
MHLW_ORIGIN = "https://www.mhlw.go.jp"

# "General scrape rules" in 01_brief.md: contact in the UA, 2 seconds or more between requests
UA = "llens-cage/0.1 (LLENS開発チーム <<redacted-email>>)"
SLEEP = 2.0

# Section heading on the special page. Only look inside this section (the word "疑義解釈" also appears elsewhere)
SECTION_ANCHOR = "３．疑義解釈・訂正通知"
# Next section heading (the window closes here)
SECTION_END = "４．給付調整"

# Labels after NFKC
SONO_RE = re.compile(
    r"^疑義解釈資料の送付について\(その(\d+)\)\(令和(\d+)年(\d+)月(\d+)日保険局医療課事務連絡\)"
)
TEISEI_RE = re.compile(
    r"^「疑義解釈資料の送付について\(その(\d+)\)」の一部訂正について"
    r"\(令和(\d+)年(\d+)月(\d+)日保険局医療課事務連絡\)"
)

REIWA_BASE = 2018  # Reiwa N = 2018 + N


def _get(session: requests.Session, url: str, *, binary: bool = False):
    r = session.get(url, timeout=120)
    r.raise_for_status()
    time.sleep(SLEEP)
    return r.content if binary else r


def _links(html: str) -> list[tuple[int, str, str]]:
    """(tag start position, label, href). The position is an offset in the raw HTML."""
    out = []
    for m in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, re.S):
        label = htmllib.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
        out.append((m.start(), label, m.group(1)))
    return out


def _sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _nfkc(s: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s))


def _date(reiwa: str, m: str, d: str) -> str:
    return dt.date(REIWA_BASE + int(reiwa), int(m), int(d)).isoformat()


def parse_page(html: str) -> tuple[list[dict], list[dict]]:
    """Special page → (picked, excluded). Both carry label / url.

    Picked entries also get kind (`sono` / `teisei`) / n (その N) / issued (issue date) / file (cache name).
    """
    lo = html.find(SECTION_ANCHOR)
    if lo < 0:
        sys.exit(f"section heading not found on the special page: {SECTION_ANCHOR!r} (page layout changed)")
    hi = html.find(SECTION_END, lo)
    if hi < 0:
        sys.exit(f"next section heading not found on the special page: {SECTION_END!r} (page layout changed)")
    picked: list[dict] = []
    excluded: list[dict] = []
    teisei_seq: dict[int, int] = {}
    for start, label, href in _links(html):
        if not (lo <= start < hi) or not href.endswith(".pdf"):
            continue
        url = MHLW_ORIGIN + href if href.startswith("/") else href
        # Drop the trailing ［519KB］ from the label before parsing
        lab = re.sub(r"[［\[][\d.]+[KM]B[］\]]$", "", _nfkc(label))
        m = SONO_RE.match(lab)
        if m:
            n = int(m.group(1))
            picked.append(
                {
                    "kind": "sono",
                    "n": n,
                    "issued": _date(*m.groups()[1:]),
                    "label": label,
                    "url": url,
                    "file": f"sono{n:02d}",
                }
            )
            continue
        m = TEISEI_RE.match(lab)
        if m:
            n = int(m.group(1))
            teisei_seq[n] = teisei_seq.get(n, 0) + 1
            picked.append(
                {
                    "kind": "teisei",
                    "n": n,
                    "issued": _date(*m.groups()[1:]),
                    "label": label,
                    "url": url,
                    "file": f"sono{n:02d}_teisei{teisei_seq[n]:02d}",
                }
            )
            continue
        excluded.append({"label": label, "url": url})
    if not picked:
        sys.exit("no Q&A link could be picked (label format changed)")
    # "その N" should run from 1 with no gaps. A gap means the page is being read wrongly
    ns = sorted(e["n"] for e in picked if e["kind"] == "sono")
    if ns != list(range(1, len(ns) + 1)):
        sys.exit(f"gap in the \"その N\" sequence: {ns}")
    for e in picked:
        if e["kind"] == "teisei" and e["n"] not in ns:
            sys.exit(f"original of the correction (その{e['n']}) is missing: {e['label']}")
    picked.sort(key=lambda e: (e["n"], e["kind"] != "sono", e["file"]))
    return picked, excluded


def _pdftotext(pdf: Path, txt: Path) -> None:
    """`pdftotext -layout`. The Q&A is plain single-column "問 N" / "(答)" text rather than
    two columns, but tables (sample forms) are mixed in, so keep the columns with -layout."""
    subprocess.run(["pdftotext", "-layout", str(pdf), str(txt)], check=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="only list the links")
    args = ap.parse_args()

    s = requests.Session()
    s.headers["User-Agent"] = UA

    print(f"[1] revision special page {KAITEI_URL}")
    r = _get(s, KAITEI_URL)
    r.encoding = r.apparent_encoding or "utf-8"
    html = r.text
    picked, excluded = parse_page(html)
    for e in picked:
        tag = f"その{e['n']}" + (" (correction)" if e["kind"] == "teisei" else "")
        print(f"    {tag:<10} {e['issued']}  {e['url']}")
    for e in excluded:
        print(f"    (excluded) {e['label'][:60]}")
    latest = max((e for e in picked if e["kind"] == "sono"), key=lambda e: e["n"])
    print(f"    latest: その{latest['n']} ({latest['issued']})")

    if args.dry_run:
        print("\n(dry-run: stopping here)")
        return

    CACHE.mkdir(parents=True, exist_ok=True)
    (CACHE / "mhlw_kaitei.html").write_text(html, encoding="utf-8")

    print("[2] PDF")
    for e in picked:
        blob = _get(s, e["url"], binary=True)
        pdf = CACHE / f"{e['file']}.pdf"
        pdf.write_bytes(blob)
        e["bytes"] = len(blob)
        e["sha256"] = _sha256(blob)
        _pdftotext(pdf, pdf.with_suffix(".txt"))
        print(f"    {pdf.name}: {len(blob):,} bytes")

    manifest = {
        "retrieved_at": dt.date.today().isoformat(),
        "page": KAITEI_URL,
        "latest": {"n": latest["n"], "issued": latest["issued"]},
        "items": picked,
        "excluded": excluded,
    }
    (CACHE / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nwrote manifest.json ({len(picked)} files, latest その{latest['n']} {latest['issued']})")


if __name__ == "__main__":
    main()
