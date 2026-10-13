#!/usr/bin/env python3
"""Fetch the source set of the medical fee schedule (ika tensu-hyo) into cache/shinryo_hoshu/.

There are **two sources**, both from MHLW. Neither alone yields a document tree:

  1. **Revision special page** <https://www.mhlw.go.jp/stf/newpage_67729.html>
     PDFs of the notification body (kokuji: points and notes) and the implementation
     notice (tsuchi: billing requirements in prose) are listed here.
     -> `kokuji.pdf` / `tsuchi.pdf` (+ `.txt` from pdftotext -layout)
  2. **Shinryo Hoshu information service** <https://shinryohoshu.mhlw.go.jp/shinryohoshu/downloadMenu/>
     Medical procedure master (CSV). Procedure code, points, chapter, part and
     section number come as columns.
     -> `masters/s.csv`

Output (cache/shinryo_hoshu/):
  - mhlw_kaitei.html   ... raw HTML of the special page (evidence of link resolution)
  - kokuji.pdf / .txt  ... fee schedule (notification body)
  - tsuchi.pdf / .txt  ... fee schedule (attached to the implementation notice)
  - masters/s.csv      ... medical procedure master (cp932, no header row)
  - layout_rec1.pdf / layout_rec3.pdf ... master spec / file layout
  - manifest.json      ... URL / bytes / sha256 / fetch date / correction-version notes

Pitfalls hit (so a re-fetch does not trip on the same spots):

  - **The kokuji PDF keeps being replaced by corrected versions.** The special page
    puts a note like `（0529訂正後）` `（0730訂正後）` right after the link. **The
    number in the link target does not reveal the version**, so the note is copied
    into the manifest for a human to check. One fetch is not the end of it (right
    after a revision it gets replaced for months)
  - **The current master link cannot be told apart by name.** The download page has
    two tables, 【令和6年度版】 and 【令和8年度版】, and **only the current one is
    `/shinryohoshu/downloadMenu/sFile`; the old one is `/shinryohoshu/file/etc/R06_s.zip`**.
    At the next revision this shifts by one (R08 drops to `file/etc/R08_s.zip`), so
    **look it up from the 「【令和N年度版】」 heading, not the link name**. Set with `--nendo`
  - **jsessionid is embedded in the URL.** Cookies alone sometimes do not pass, so
    use the `;jsessionid=...` URLs that appear in the page as they are
  - **The CSV is Shift_JIS (cp932) with no header row.** Column meanings are only
    known by matching against layout_rec3.pdf (file layout), hence the spec is fetched too
  - **Gigi kaishaku (Q&A interpretations) are never merged into the body.** They just
    pile up on the special page as その1〜その12. Not fetched here (out of scope).
    Only the link list is kept in the manifest

usage:
    uv run --group ingest python scripts/fetch_shinryo_hoshu.py            # fetch everything
    uv run --group ingest python scripts/fetch_shinryo_hoshu.py --dry-run  # print URLs only
    uv run --group ingest python scripts/fetch_shinryo_hoshu.py --nendo 令和10年度
  (run from cage/)
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html as htmllib
import io
import json
import re
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import requests

CACHE = Path("cache/shinryo_hoshu")

KAITEI_URL = "https://www.mhlw.go.jp/stf/newpage_67729.html"
MHLW_ORIGIN = "https://www.mhlw.go.jp"
MASTER_MENU = "https://shinryohoshu.mhlw.go.jp/shinryohoshu/downloadMenu/"
MASTER_ORIGIN = "https://shinryohoshu.mhlw.go.jp"

# Current revision. The master download page splits tables by 【令和N年度版(...)】
NENDO = "令和8年度"

# 01_brief.md "general scraping rules": contact in UA, interval >= 2 s
UA = "llens-cage/0.1 (LLENS開発チーム <<redacted-email>>)"
SLEEP = 2.0

# Anchors for finding the kokuji/tsuchi fee schedule on the special page. **The link
# name (「医科点数表」) appears under both**, so scope by the preceding heading
KOKUJI_ANCHOR = "診療報酬の算定方法の一部を改正する件"
TSUCHI_ANCHOR = "実施上の留意事項について"

# Master spec. The relative path in the page moves per revision, e.g. R08rec1 -> R10rec1
SPEC_RE = re.compile(r"/shinryohoshu/file/spec/(R\d+rec[13])\.pdf")


def _get(session: requests.Session, url: str, *, binary: bool = False):
    r = session.get(url, timeout=120)
    r.raise_for_status()
    time.sleep(SLEEP)
    return r.content if binary else r


def _flat(html: str) -> str:
    """Plain text with tags collapsed to `|`. Used to inspect link context by offset."""
    return htmllib.unescape(re.sub(r"<[^>]+>", "|", html))


def _links(html: str) -> list[tuple[int, str, str]]:
    """List of (tag start, label, href). Position is an offset into the raw HTML."""
    out = []
    for m in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, re.S):
        label = htmllib.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
        out.append((m.start(), label, m.group(1)))
    return out


def _sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# ---------------------------------------------------------------------------
# 1. Revision special page -> kokuji / implementation notice PDFs
# ---------------------------------------------------------------------------


def _is_ika(label: str) -> bool:
    """Is the link label the fee schedule body? **No prefix match** --
    unrelated ones like 「医科点数表第10部の区分番号（Kコード）に対応する…」 would slip in."""
    return label == "医科点数表" or label.startswith("医科点数表［")


def _pick_ika(html: str, anchor: str) -> tuple[str, str, str]:
    """Return the first 「医科点数表」 link after the `anchor` heading -> (label, url, correction note).

    **Scoping by heading is the key.** The label 「医科点数表」 appears under both the
    kokuji and the tsuchi, so picking by label alone grabs the wrong one.

    The correction note (`（0730訂正後）`) sits in the **prose right after the link**.
    Taking a window like "next 1200 chars" here **runs into the next kokuji/tsuchi
    heading and picks up another notification's correction note as the body's**
    (observed: the kokuji got the tsuchi-side `（0529訂正後）`). Always cut the
    window **before the next link**.
    """
    pos = html.find(anchor)
    if pos < 0:
        sys.exit(f"heading not found on the special page: {anchor!r} (page layout changed)")
    links = _links(html)
    cands = [
        (i, s, lab, href)
        for i, (s, lab, href) in enumerate(links)
        if s > pos and _is_ika(lab) and href.endswith(".pdf")
    ]
    if not cands:
        sys.exit(f"no 「医科点数表」 PDF link after {anchor!r}")
    idx, start, label, href = cands[0]
    end = links[idx + 1][0] if idx + 1 < len(links) else len(html)
    note = "".join(re.findall(r"（\d{4}訂正後）", _flat(html[start:end]))[:1])
    # Two 医科点数表 links in the same group (up to the next 「制定文」 link)
    # = a corrected version listed alongside. Do not auto-pick the newer one --
    # the page note decides which is latest, so a human checks
    grp_end = next(
        (s for s, lab, _h in links if s > start and lab.startswith("制定文")), len(html)
    )
    same = [c for c in cands if start <= c[1] < grp_end]
    if len(same) > 1:
        print(
            f"  ! group 「{anchor}」 has {len(same)} 医科点数表 links. "
            f"A corrected version may be listed alongside: {[c[3] for c in same]}",
            file=sys.stderr,
        )
    return label, MHLW_ORIGIN + href if href.startswith("/") else href, note


def _giginkaishaku(html: str) -> list[dict]:
    """Link list of gigi kaishaku (not ingested; kept as evidence they are still being issued)."""
    out = []
    for _s, label, href in _links(html):
        if "疑義解釈" in label and href.endswith(".pdf"):
            out.append(
                {"label": label, "url": MHLW_ORIGIN + href if href.startswith("/") else href}
            )
    return out


# ---------------------------------------------------------------------------
# 2. Shinryo Hoshu information service -> medical procedure master
# ---------------------------------------------------------------------------


def _master_section(html: str, nendo: str) -> tuple[int, int]:
    """Return the range from the 【令和N年度版(...)】 heading to the next heading.

    **The old version's table comes first**, so searching for 「医科診療行為マスター」
    without scoping grabs the old one (R06_s.zip).
    """
    heads = [m.start() for m in re.finditer(r"【令和\s*\d+\s*年度版", html)]
    if not heads:
        sys.exit("no 【令和N年度版】 heading on the download page (layout changed)")
    want = re.sub(r"令和(\d+)年度", r"令和\\s*\1\\s*年度版", re.escape(nendo).replace("\\", ""))
    for i, h in enumerate(heads):
        if re.match(want, html[h + 1 : h + 24]):
            return h, (heads[i + 1] if i + 1 < len(heads) else len(html))
    sys.exit(
        f"table for 【{nendo}版】 not found. The page has "
        f"{[html[h:h+16] for h in heads]}"
    )


def _master_row(html: str, lo: int, hi: int) -> tuple[str, str, str]:
    """(download URL, record count, last-updated date) of the medical procedure master."""
    seg = html[lo:hi]
    m = re.search(r"医科診療行為マスター", seg)
    if not m:
        sys.exit("no 「医科診療行為マスター」 in that fiscal-year table")
    # The link is on the name cell itself or the <a> just before it. Search +-3000 chars
    around = seg[max(0, m.start() - 3000) : m.start() + 3000]
    hrefs = [
        h
        for _s, _l, h in _links(around)
        if re.search(r"(downloadMenu/sFile|file/etc/R\d+_s\.zip)", h)
    ]
    if not hrefs:
        sys.exit("cannot find the download link of the medical procedure master")
    tail = _flat(seg[m.start() : m.start() + 400])
    cnt = (re.search(r"([\d,]+)件", tail) or [None, ""])[1]
    upd = (re.search(r"令和\s*\d+\s*年\s*\d+\s*月\s*\d+\s*日", tail) or [""])[0]
    return MASTER_ORIGIN + hrefs[0], cnt, re.sub(r"\s+", "", upd)


def _unzip_single_csv(blob: bytes) -> tuple[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        names = [n for n in z.namelist() if n.lower().endswith(".csv")]
        if len(names) != 1:
            sys.exit(f"zip does not contain exactly one CSV: {names}")
        return names[0], z.read(names[0])


# ---------------------------------------------------------------------------


def _pdftotext(pdf: Path, txt: Path) -> None:
    """`pdftotext -layout`. **-layout is required** -- the fee schedule expresses hierarchy
    by column alignment; plain extraction breaks the note-number/body correspondence."""
    subprocess.run(["pdftotext", "-layout", str(pdf), str(txt)], check=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print URLs only")
    ap.add_argument("--nendo", default=NENDO, help="fiscal-year edition of the master (default: %(default)s)")
    args = ap.parse_args()

    CACHE.mkdir(parents=True, exist_ok=True)
    (CACHE / "masters").mkdir(exist_ok=True)
    s = requests.Session()
    s.headers["User-Agent"] = UA

    manifest: dict = {
        "retrieved_at": dt.date.today().isoformat(),
        "nendo": args.nendo,
        "sources": {},
    }

    print(f"[1] revision special page {KAITEI_URL}")
    kaitei = _get(s, KAITEI_URL)
    kaitei.encoding = kaitei.apparent_encoding or "utf-8"
    khtml = kaitei.text
    k_label, k_url, k_note = _pick_ika(khtml, KOKUJI_ANCHOR)
    t_label, t_url, t_note = _pick_ika(khtml, TSUCHI_ANCHOR)
    print(f"    kokuji {k_label} {k_note} -> {k_url}")
    print(f"    tsuchi {t_label} {t_note} -> {t_url}")

    print(f"[2] master download page {MASTER_MENU}")
    menu = _get(s, MASTER_MENU)
    menu.encoding = "cp932"  # **no auto-detection** (fixed to Shift_JIS)
    mhtml = menu.text
    lo, hi = _master_section(mhtml, args.nendo)
    m_url, m_cnt, m_upd = _master_row(mhtml, lo, hi)
    print(f"    master {m_cnt} records / last updated {m_upd} -> {m_url}")
    specs = {
        name: MASTER_ORIGIN + f"/shinryohoshu/file/spec/{name}.pdf"
        for name in sorted(set(SPEC_RE.findall(mhtml)))
    }
    print(f"    specs {list(specs)}")

    if args.dry_run:
        print("\n(dry-run: stopping here)")
        return

    (CACHE / "mhlw_kaitei.html").write_text(khtml, encoding="utf-8")
    (CACHE / "master_menu.html").write_text(mhtml, encoding="utf-8")

    def save(name: str, url: str, note: str = "") -> None:
        blob = _get(s, url, binary=True)
        (CACHE / name).write_bytes(blob)
        manifest["sources"][name] = {
            "url": url,
            "bytes": len(blob),
            "sha256": _sha256(blob),
            "note": note,
        }
        print(f"    {name}: {len(blob):,} bytes")

    print("[3] PDF")
    save("kokuji.pdf", k_url, f"{k_label} {k_note}".strip())
    save("tsuchi.pdf", t_url, f"{t_label} {t_note}".strip())
    for name, url in specs.items():
        save(f"layout_{name[-4:]}.pdf", url, name)

    print("[4] medical procedure master")
    blob = _get(s, m_url, binary=True)
    inner, csv_bytes = _unzip_single_csv(blob)
    (CACHE / "masters" / "s.csv").write_bytes(csv_bytes)
    manifest["sources"]["masters/s.csv"] = {
        "url": m_url,
        "bytes": len(csv_bytes),
        "sha256": _sha256(csv_bytes),
        "note": f"{inner} / {m_cnt}件 / 最終更新 {m_upd} / {args.nendo}版",
    }
    print(f"    masters/s.csv: {len(csv_bytes):,} bytes ({inner})")

    print("[5] pdftotext -layout")
    for stem in ("kokuji", "tsuchi"):
        _pdftotext(CACHE / f"{stem}.pdf", CACHE / f"{stem}.txt")
        print(f"    {stem}.txt: {(CACHE / f'{stem}.txt').stat().st_size:,} bytes")

    manifest["giginkaishaku"] = _giginkaishaku(khtml)
    (CACHE / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        f"\nwrote manifest.json. {len(manifest['giginkaishaku'])} gigi kaishaku links "
        "(**not ingested**; they are never merged into the body, see the originals)"
    )


if __name__ == "__main__":
    main()
