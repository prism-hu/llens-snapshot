#!/usr/bin/env python3
"""Fetch the DPC (diagnosis procedure combination) electronic fee table and related sources into cache/dpc/.

Single source: the MHLW FY2026 (令和8年度) revision special page
<https://www.mhlw.go.jp/stf/newpage_67729.html>, sections 「第６ 電子点数表等」 and 「８．ＤＰＣ」:

  1. **DPC electronic fee table (xlsx)** ... the fee table body. Definition tables for
     classification / ICD / surgery / procedure / comorbidity / severity and the points per
     14-digit code, all in one workbook -> `dpc.xlsx` + `dpc.json`
  2. **Coefficient notification, appendix tables 1-6 (xlsx)** ... per-hospital basic
     coefficient, function evaluation coefficient II, emergency adjustment, transition
     mitigation (tables 1-3) and function evaluation coefficient I (tables 4-6)
     -> `keisu_beppyo.xlsx` + `keisu.json`
  3. **Implementation notice** (算定方法の一部改正等に伴う実施上の留意事項について, 保医発0318第4号) ...
     eligible patients, how DPC codes are decided, bundled scope, stay periods, readmission
     rules -> `ryui_tsuchi.pdf/.txt`
  4. **Participation procedure notice** (DPC制度への参加等の手続きについて, 保医発0327第1号) ...
     criteria for DPC hospitals, joining, leaving -> `sanka_tsuchi.pdf/.txt`
  5. **Billing notification** (厚生労働大臣が指定する病院の病棟における療養に要する費用の額の算定方法の
     一部を改正する件, MHLW notification No. 96 of 2026, 114 p) ... appendix items 1-19
     (definition of standard points, costs outside the bundle, add-on / reduction tables for
     specific inpatient fees, beyond period III, adjustment days) -> `kokuji96.pdf/.txt`
     + `kokuji96_lines.json` (pdfplumber words per line with x coords, to rebuild 2-column tables)

Output (cache/dpc/):
  - mhlw_kaitei.html      ... raw HTML of the special page (evidence for version selection)
  - dpc.xlsx / dpc.json   ... the fee table original and a JSON of all sheets as string matrices
                             (build / verify read only the JSON = standard library only)
  - keisu_beppyo.xlsx / keisu.json
  - ryui_tsuchi.pdf / .txt / sanka_tsuchi.pdf / .txt   (pdftotext -layout)
  - manifest.json         ... URL / bytes / sha256 / fetch date / **selected version and candidate list**

Pitfalls hit:

  - **The electronic fee table has many links with the same name.** FY2026 has 1 「暫定版」
    (provisional) + 6 「正式版」 (official) (updated 3/18, 4/15, 5/19 x2, 7/14, 8/12). Link names
    and sizes are nearly identical; **the version is only visible in the prose right after the
    link, 「（令和８年８月12日更新）」**. Selection rules are in the `_pick_dpc_xlsx` docstring
  - **5/19 has two on the same day** (2.6MB 「※前回掲載データの一部修正」 and 2.4MB). On a tie in
    update date, take **the one later on the page**
  - **The billing notification (No. 96) PDF is vertical text** and `pdftotext` breaks it column by
    column. The fee table body is the same as the xlsx, so it is not taken (same treatment as the
    facility standards notification; `docs/cage/20_shisetsu_kijun.md`)
  - The coefficient notification (No. 99) body PDF is vertical too. The **appendix xlsx** has the
    per-hospital coefficients in horizontal text, so take that (link just under the notification,
    「別表第一～第六」)
  - **In the billing notification (No. 96) only pages 1-9 (amendment text, transitional measures)
    are vertical.** Pages 10-36 are horizontal appendices (items 1-19); page 37 onward is the
    landscape DPC fee table (same as the xlsx, so discarded). Page numbers are not hard-coded:
    **vertical pages are detected by pdfplumber's mean word length (1 char)**, and build cuts the
    body at 「１ １日当たりの療養に…」 and 「19 診断群分類点数表は、次のとおり」

usage:
    uv run --group ingest python scripts/fetch_dpc.py            # fetch everything
    uv run --group ingest python scripts/fetch_dpc.py --dry-run  # print version selection and URLs only
  (run from cage/. openpyxl / requests are in --group ingest. pdftotext (poppler-utils) is required)
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
from pathlib import Path

import requests

CACHE = Path("cache/dpc")

KAITEI_URL = "https://www.mhlw.go.jp/stf/newpage_67729.html"
MHLW_ORIGIN = "https://www.mhlw.go.jp"

# 01_brief.md "general scraping rules": contact in UA, interval >= 2 s
UA = "llens-cage/0.1 (LLENS開発チーム <<redacted-email>>)"
SLEEP = 2.0

DPC_XLSX_LABEL = "診断群分類（DPC）電子点数表（正式版）"
DPC_BLOCK_START = "診断群分類（DPC）電子点数表"  # subheading under 「第６ 電子点数表等」
DPC_BLOCK_END = "コーディングテキスト"  # next subheading
KEISU_KOKUJI = "基礎係数、機能評価係数Ⅰ、機能評価係数Ⅱ、救急補正係数及び激変緩和係数の一部を改正する件"
KEISU_BEPPYO_LABEL = "別表第一～第六"
RYUI_LABEL = "算定方法の一部改正等に伴う実施上の留意事項について"
SANKA_LABEL = "DPC制度への参加等の手続きについて"
# Billing notification. Two notifications start the same way (No. 96 body / No. 97 amending
# the item-5 notification), so match through 「…算定方法の一部を改正する件」 (No. 97 reads 「…第一項第五号の規定に基づき…」)
KOKUJI96_LABEL = "療養に要する費用の額の算定方法の一部を改正する件"

_WAREKI = re.compile(r"令和\s*(\d+)\s*年\s*(\d+)\s*月\s*(\d+)\s*日\s*更新")


def _get(session: requests.Session, url: str, *, binary: bool = False):
    r = session.get(url, timeout=120)
    r.raise_for_status()
    time.sleep(SLEEP)
    return r.content if binary else r


def _flat(html: str) -> str:
    return htmllib.unescape(re.sub(r"<[^>]+>", "|", html))


def _links(html: str) -> list[tuple[int, str, str]]:
    """(tag start, label, href). Position is an offset into the raw HTML."""
    out = []
    for m in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, re.S):
        label = htmllib.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
        out.append((m.start(), label, m.group(1)))
    return out


def _abs(href: str) -> str:
    return MHLW_ORIGIN + href if href.startswith("/") else href


def _sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _wareki_to_iso(note: str) -> str | None:
    m = _WAREKI.search(note)
    if not m:
        return None
    y, mo, d = (int(x) for x in m.groups())
    return f"{2018 + y:04d}-{mo:02d}-{d:02d}"


# ---------------------------------------------------------------------------
# Version selection
# ---------------------------------------------------------------------------


def _pick_dpc_xlsx(html: str) -> tuple[dict, list[dict]]:
    """Pick the **latest official version** of the electronic fee table. Returns (chosen, candidates).

    Rules (same wording as `docs/cage/21_dpc.md` "the one road for re-fetching"):

      1. Only xlsx links between the special page subheading 「診断群分類（DPC）電子点数表」
         and the next subheading 「コーディングテキスト」 are candidates
      2. Keep only labels that **start with 「診断群分類（DPC）電子点数表（正式版）」**
         (drops 「（暫定版）」 and 「（暫定版）からの修正履歴」)
      3. Read 「（令和N年M月D日更新）」 from the **prose right after** each link (up to the
         next link) and take **the most recent update date**
      4. On an equal date (two on the same day, like 5/19) or when no note is readable,
         take **the one later on the page** (the page is in chronological order)

    Stops if the page layout changed and there are 0 candidates (never silently takes something else).
    """
    lo = html.find(DPC_BLOCK_START)
    if lo < 0:
        sys.exit(f"subheading not found on the special page: {DPC_BLOCK_START!r} (page layout changed)")
    hi = html.find(DPC_BLOCK_END, lo)
    if hi < 0:
        hi = len(html)
    links = _links(html)
    cands = []
    for i, (s, label, href) in enumerate(links):
        if not (lo <= s < hi) or not href.endswith(".xlsx"):
            continue
        end = links[i + 1][0] if i + 1 < len(links) else len(html)
        tail = re.sub(r"[\s|]+", " ", _flat(html[s:end]))
        # The note is 「（令和８年８月12日更新）」 + optional 「※前回掲載データの一部修正」.
        # Beyond that the next subheading (コーディングテキスト) bleeds in, so stop there
        m = re.search(r"（令和[^）]*更新）(?:\s*※[^ ・|]*)?", tail)
        note = m.group(0).strip() if m else ""
        cands.append(
            {
                "label": label,
                "url": _abs(href),
                "note": note,
                "updated": _wareki_to_iso(note),
                "official": label.startswith(DPC_XLSX_LABEL),
                "order": len(cands) + 1,
            }
        )
    official = [c for c in cands if c["official"]]
    if not official:
        sys.exit(f"no official-version link of the electronic fee table. candidates: {[c['label'] for c in cands]}")
    # Max by (update date, or minimum if none) then page order, lexicographically
    best = max(official, key=lambda c: (c["updated"] or "", c["order"]))
    best = dict(best, rank=f"正式版 {official.index(best) + 1}/{len(official)} 本目")
    return best, cands


def _pick_after(html: str, anchor: str, label_prefix: str, ext: str) -> tuple[str, str]:
    """First link after the string `anchor` whose label starts with `label_prefix`."""
    pos = html.find(anchor)
    if pos < 0:
        sys.exit(f"heading not found on the special page: {anchor!r}")
    for s, label, href in _links(html):
        if s > pos and label.startswith(label_prefix) and href.endswith(ext):
            return label, _abs(href)
    sys.exit(f"no {label_prefix!r} link after {anchor!r}")


def _pick_label(html: str, needle: str, ext: str) -> tuple[str, str]:
    """First link whose label contains `needle`."""
    for _s, label, href in _links(html):
        if needle in label and href.endswith(ext):
            return label, _abs(href)
    sys.exit(f"no link whose label contains {needle!r}")


# ---------------------------------------------------------------------------
# xlsx -> JSON (so build / verify run on the standard library only)
# ---------------------------------------------------------------------------


def _cell(v) -> str:
    """Cell value to string. Integral numbers in integer form (so points 3556 / date 20260601
    do not become floats). None and whitespace-only strings become ''."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else repr(v)
    if isinstance(v, int):
        return str(v)
    if isinstance(v, (dt.date, dt.datetime)):
        return v.strftime("%Y%m%d")
    return str(v).strip()


def xlsx_to_json(src: Path, dst: Path) -> dict:
    """Dump all sheets as {sheet name: [[cell string, ...], ...]}. **Sheet names are stripped**
    (the original has trailing spaces like '６）手術 '). Trailing empty cells are kept and
    column counts are padded within a sheet (lookups are by column position)."""
    import openpyxl

    wb = openpyxl.load_workbook(src, read_only=True, data_only=True)
    out: dict[str, list[list[str]]] = {}
    for ws in wb.worksheets:
        rows = [[_cell(c) for c in r] for r in ws.iter_rows(values_only=True)]
        width = max((len(r) for r in rows), default=0)
        rows = [r + [""] * (width - len(r)) for r in rows]
        # Drop fully empty rows (junk at the end). Empty rows in the middle go too, but
        # in every sheet row meaning is decided by columns, not by position
        rows = [r for r in rows if any(r)]
        out[ws.title.strip()] = rows
    dst.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return {name: len(rows) for name, rows in out.items()}


def pdf_lines_json(src: Path, dst: Path) -> dict:
    """Dump words of all pages grouped into lines via pdfplumber. **Vertical-text pages have a mean
    word length of 1 char** (chars stacked vertically), so they are marked by `kind`, as are
    landscape pages (fee table). build picks ranges by those marks and body cut points (no hard-coded page numbers)."""
    import statistics

    import pdfplumber

    pages = []
    with pdfplumber.open(str(src)) as pdf:
        for i, pg in enumerate(pdf.pages):
            words = pg.extract_words()
            avg = statistics.mean(len(w["text"]) for w in words) if words else 0
            if pg.width > pg.height:
                kind = "landscape"
            elif avg < 3:
                kind = "vertical"
            else:
                kind = "body"
            lines: dict[int, list] = {}
            for w in words:
                key = round(w["top"])
                # top wobbles by 1-2 pt within a line, so snap to a nearby line
                near = next((k for k in lines if abs(k - key) <= 2), key)
                lines.setdefault(near, []).append([round(w["x0"], 1), round(w["x1"], 1), w["text"]])
            pages.append(
                {
                    "page": i + 1,
                    "kind": kind,
                    "lines": [sorted(ws, key=lambda w: w[0]) for _t, ws in sorted(lines.items())],
                }
            )
    dst.write_text(json.dumps(pages, ensure_ascii=False), encoding="utf-8")
    kinds: dict[str, int] = {}
    for p in pages:
        kinds[p["kind"]] = kinds.get(p["kind"], 0) + 1
    return kinds


def _pdftotext(pdf: Path, txt: Path) -> None:
    subprocess.run(["pdftotext", "-layout", str(pdf), str(txt)], check=True)


# ---------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print version selection and URLs only")
    args = ap.parse_args()

    CACHE.mkdir(parents=True, exist_ok=True)
    s = requests.Session()
    s.headers["User-Agent"] = UA

    print(f"[1] revision special page {KAITEI_URL}")
    page = _get(s, KAITEI_URL)
    page.encoding = page.apparent_encoding or "utf-8"
    html = page.text

    best, cands = _pick_dpc_xlsx(html)
    print("    electronic fee table candidates:")
    for c in cands:
        mark = "→" if c is best or c["url"] == best["url"] else " "
        print(f"     {mark} {c['order']}. {c['label']} {c['note']} {c['url']}")
    print(f"    chosen: {best['rank']} / updated {best['updated']} / {best['url']}")

    k_label, k_url = _pick_after(html, KEISU_KOKUJI, KEISU_BEPPYO_LABEL, ".xlsx")
    r_label, r_url = _pick_label(html, RYUI_LABEL, ".pdf")
    p_label, p_url = _pick_label(html, SANKA_LABEL, ".pdf")
    g_label, g_url = _pick_label(html, KOKUJI96_LABEL, ".pdf")
    print(f"    coeff tables  {k_label} -> {k_url}")
    print(f"    impl notice   {r_label} -> {r_url}")
    print(f"    participation {p_label} -> {p_url}")
    print(f"    billing kokuji {g_label} -> {g_url}")

    if args.dry_run:
        print("\n(dry-run: stopping here)")
        return

    (CACHE / "mhlw_kaitei.html").write_text(html, encoding="utf-8")
    manifest: dict = {
        "retrieved_at": dt.date.today().isoformat(),
        "page": KAITEI_URL,
        "dpc_version": {k: best[k] for k in ("label", "note", "updated", "rank", "url")},
        "dpc_candidates": cands,
        "sources": {},
    }

    def save(name: str, url: str, note: str) -> Path:
        blob = _get(s, url, binary=True)
        (CACHE / name).write_bytes(blob)
        manifest["sources"][name] = {
            "url": url,
            "bytes": len(blob),
            "sha256": _sha256(blob),
            "note": note,
        }
        print(f"    {name}: {len(blob):,} bytes")
        return CACHE / name

    print("[2] fetch")
    save("dpc.xlsx", best["url"], f"{best['label']} {best['note']} / {best['rank']}")
    save("keisu_beppyo.xlsx", k_url, f"{k_label} (令和8年厚生労働省告示第99号の別表)")
    save("ryui_tsuchi.pdf", r_url, r_label)
    save("sanka_tsuchi.pdf", p_url, p_label)
    save("kokuji96.pdf", g_url, g_label)

    print("[3] xlsx -> json")
    for stem in ("dpc", "keisu_beppyo"):
        counts = xlsx_to_json(CACHE / f"{stem}.xlsx", CACHE / f"{stem}.json")
        manifest["sources"][f"{stem}.json"] = {"sheets": counts}
        print(f"    {stem}.json: " + ", ".join(f"{k}={v}" for k, v in counts.items()))

    print("[4] pdftotext -layout")
    for stem in ("ryui_tsuchi", "sanka_tsuchi", "kokuji96"):
        _pdftotext(CACHE / f"{stem}.pdf", CACHE / f"{stem}.txt")
        print(f"    {stem}.txt: {(CACHE / f'{stem}.txt').stat().st_size:,} bytes")
    kinds = pdf_lines_json(CACHE / "kokuji96.pdf", CACHE / "kokuji96_lines.json")
    manifest["sources"]["kokuji96_lines.json"] = {"pages": kinds}
    print(f"    kokuji96_lines.json: page kinds {kinds}")

    (CACHE / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("\nwrote manifest.json")


if __name__ == "__main__":
    main()
