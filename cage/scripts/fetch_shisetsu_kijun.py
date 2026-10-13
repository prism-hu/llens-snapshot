#!/usr/bin/env python3
"""Fetch the full set of shisetsu kijun (facility standards) source documents into cache/shisetsu_kijun/.

There are **two sources**, opposite in nature:

  1. **MHLW revision special page** <https://www.mhlw.go.jp/stf/newpage_67729.html> (PDL1.0, may leave the hospital)
     The contents of "別に厚生労働大臣が定める施設基準" = the two facility-standard circulars.
       - 基本診療料の施設基準等及びその届出に関する手続きの取扱いについて (保医発0305第7号)
       - 特掲診療料の施設基準等及びその届出に関する手続きの取扱いについて (保医発0305第8号)
     → `kihon.pdf` / `tokkei.pdf` (+ `.txt` from pdftotext -layout)
     The facility-standard notification checklist (an administrative notice) is also downloaded for
     reference but **not used by the build** (it is a clerical list of new / changed facility standards
     sorted by notification deadline, not the contents of the standards)
  2. **Hospital intranet his-info** <https://<redacted-intranet-host>/his-info/top.aspx> (**must not leave the hospital**)
     The "施設基準に関する職員の登録状況" link on the top page. It is actually the
     **"施設基準登録者一覧"** (facility-standard registrant list) of the Medical Affairs Division
     (the roster of staff registered as dedicated / full-time for each facility standard).
     → `touroku.pdf` + `touroku.json` with the table extracted

Output (cache/shisetsu_kijun/):
  - mhlw_kaitei.html   ... raw HTML of the special page (evidence of link resolution)
  - kihon.pdf / .txt   ... basic medical fee (基本診療料) facility-standard circular
  - tokkei.pdf / .txt  ... specific medical fee (特掲診療料) facility-standard circular
  - checklist_*.pdf    ... facility-standard notification checklist (reference, not ingested)
  - his_top.html       ... raw HTML of the his-info top page
  - touroku.pdf        ... facility-standard registrant list (intranet)
  - touroku.json       ... the table above (rows extracted with pdfplumber; input for build / verify)
  - manifest.json      ... URL / bytes / sha256 / fetch date / note on the corrected version

Pitfalls hit (so a re-fetch does not trip over the same spots):

  - **The circular PDFs keep being replaced with corrected versions.** The special page puts a
    `（0730訂正後）` note right after the link. As in `fetch_shinryo_hoshu.py`, the window for the note
    is cut **just before the next link** (widening it picks up the note of the neighbouring attachment)
  - **The his-info link is a `__doPostBack`, and the response is HTML, not a PDF.**
    That HTML has `window.open('log_write.aspx?…&LI_URL=./sisetukijun_list.pdf')` injected,
    and a GET of it in the same session returns the PDF
    (same construction as `fetch_his_faq.py` for the FAQ). The filename is fixed, but **do not
    hit the direct URL without going through the link** (log_write is designed to record views)
  - **The registrant list is a PDF exported from Excel.** With `pdftotext -layout` the column widths
    change per page, and spaces inside the category column (`A207 1` / `A000 注1,A002 注2`) cannot be
    told apart from column boundaries. Use **pdfplumber table extraction** from the ruling lines
    (fixed 6 columns across all 38 pages, 0 missing)
  - **The registrant list is not a "list of notified facility standards".** As the title says it is
    **staff registration** (roster of dedicated / full-time staff), so facility standards that need no
    staffing (regional add-on, care environment add-on, etc.) are not listed even if notified. This is
    the basis for the disclaimer on the build side, so it is kept here

usage:
    uv run --group ingest python scripts/fetch_shisetsu_kijun.py            # fetch everything
    uv run --group ingest python scripts/fetch_shisetsu_kijun.py --dry-run  # only print the URLs
    uv run --group ingest python scripts/fetch_shisetsu_kijun.py --skip-his # skip the intranet side
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
from pathlib import Path

import requests

CACHE = Path("cache/shisetsu_kijun")

KAITEI_URL = "https://www.mhlw.go.jp/stf/newpage_67729.html"
MHLW_ORIGIN = "https://www.mhlw.go.jp"
HIS_TOP = "https://<redacted-intranet-host>/his-info/top.aspx"
HIS_ORIGIN = "https://<redacted-intranet-host>/his-info/"
HIS_LINK_TEXT = "施設基準に関する職員の登録状況"

# "General scrape rules" in 01_brief.md: contact in the UA, 2 seconds or more between requests
UA = "llens-cage/0.1 (LLENS開発チーム <<redacted-email>>)"
SLEEP = 2.0

# Link labels on the special page (prefix match). The notifications (No. 70 / No. 71) are vertical-text
# PDFs whose text collapses column by column, so they are not fetched — the circular attachments carry the same content horizontally
KIHON_LABEL = "基本診療料の施設基準等及びその届出に関する手続きの取扱いについて"
TOKKEI_LABEL = "特掲診療料の施設基準等及びその届出に関する手続きの取扱いについて"
CHECKLIST_LABEL = "施設基準届出チェックリスト"

NOTE_RE = re.compile(r"（\d{4}訂正後）")


def _get(session: requests.Session, url: str, *, binary: bool = False, **kw):
    r = session.get(url, timeout=180, **kw)
    r.raise_for_status()
    time.sleep(SLEEP)
    return r.content if binary else r


def _flat(html: str) -> str:
    return htmllib.unescape(re.sub(r"<[^>]+>", "|", html))


def _links(html: str) -> list[tuple[int, str, str]]:
    """(tag start position, label, href). The position is an offset in the raw HTML."""
    out = []
    for m in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, re.S):
        label = htmllib.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
        out.append((m.start(), label, m.group(1)))
    return out


def _sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _abs(href: str) -> str:
    return MHLW_ORIGIN + href if href.startswith("/") else href


# ---------------------------------------------------------------------------
# 1. Revision special page → facility-standard circular PDFs
# ---------------------------------------------------------------------------


def _pick(html: str, label_prefix: str) -> tuple[str, str, str]:
    """First PDF link whose label starts with `label_prefix` → (label, url, correction note).

    The correction note is in the running text right after the link. The window ends **just before the next link**.
    """
    links = _links(html)
    cands = [
        (i, s, lab, href)
        for i, (s, lab, href) in enumerate(links)
        if lab.startswith(label_prefix) and href.endswith(".pdf")
    ]
    if not cands:
        sys.exit(f"no PDF link for {label_prefix!r} on the special page (page layout changed)")
    if len(cands) > 1:
        print(
            f"  ! {len(cands)} links for {label_prefix!r}. A corrected version may be listed"
            f" alongside: {[c[3] for c in cands]} — taking the first",
            file=sys.stderr,
        )
    idx, start, label, href = cands[0]
    end = links[idx + 1][0] if idx + 1 < len(links) else len(html)
    note = "".join(NOTE_RE.findall(_flat(html[start:end]))[:1])
    return label, _abs(href), note


def _checklists(html: str) -> list[tuple[str, str, str]]:
    """Administrative notices for the facility-standard notification checklist (main PDF only; attached Excel etc. excluded).

    A date follows the label in the form `（令和８年４月20日保険局医療課事務連絡）`.
    """
    links = _links(html)
    out = []
    for i, (s, lab, href) in enumerate(links):
        if CHECKLIST_LABEL not in lab or not href.endswith(".pdf"):
            continue
        end = links[i + 1][0] if i + 1 < len(links) else len(html)
        tail = _flat(html[s:end])
        m = re.search(r"令和\s*\d+\s*年\s*\d+\s*月\s*\d+\s*日", tail)
        date = re.sub(r"\s+", "", m.group(0)) if m else ""
        out.append((lab, _abs(href), date))
    return out


# ---------------------------------------------------------------------------
# 2. his-info → facility-standard registrant list (postback → window.open → log_write.aspx)
# ---------------------------------------------------------------------------


def _hidden(name: str, html: str) -> str:
    m = re.search(r'id="%s"[^>]*value="([^"]*)"' % name, html)
    return m.group(1) if m else ""


def fetch_his(s: requests.Session, dry: bool) -> tuple[bytes | None, str, str]:
    """(PDF bytes, resolved PDF URL, top-page HTML). bytes is None when dry."""
    s.headers["User-Agent"] = "Mozilla/5.0"  # the intranet ignores the UA, but match the FAQ fetch
    r = s.get(HIS_TOP, timeout=60)
    r.raise_for_status()
    r.encoding = "utf-8"
    top = r.text
    m = re.search(
        r"__doPostBack\(&#39;([^&]+)&#39;,&#39;&#39;\)\"[^>]*>\s*" + re.escape(HIS_LINK_TEXT),
        top,
    ) or re.search(
        r"__doPostBack\('([^']+)',''\)\"[^>]*>\s*" + re.escape(HIS_LINK_TEXT), top
    )
    if not m:
        sys.exit(f"no postback link \"{HIS_LINK_TEXT}\" on the his-info top page")
    target = m.group(1)
    print(f"    postback target: {target}")
    if dry:
        return None, f"{HIS_TOP} → __doPostBack({target!r})", top
    data = {"__EVENTTARGET": target, "__EVENTARGUMENT": ""}
    for n in (
        "__VIEWSTATE", "__VIEWSTATEGENERATOR", "__VIEWSTATEENCRYPTED",
        "__PREVIOUSPAGE", "__EVENTVALIDATION",
    ):
        data[n] = _hidden(n, top)
    r2 = s.post(HIS_TOP, data=data, timeout=120)
    r2.raise_for_status()
    if r2.content[:4] == b"%PDF":
        return r2.content, HIS_TOP, top
    r2.encoding = "utf-8"
    wm = re.search(r"window\.open\('(log_write\.aspx\?[^']+)'\)", r2.text)
    if not wm:
        sys.exit("no window.open('log_write.aspx?…') in the postback response — page structure changed")
    url = HIS_ORIGIN + htmllib.unescape(wm.group(1))
    print(f"    window.open → {url}")
    r3 = s.get(url, timeout=180)
    r3.raise_for_status()
    if r3.content[:4] != b"%PDF":
        sys.exit(f"{url} did not return a PDF (Content-Type {r3.headers.get('Content-Type')})")
    return r3.content, url, top


def extract_touroku(pdf: Path) -> dict:
    """Extract the registrant-list table with pdfplumber.

    The Excel export rules every row, so `extract_tables()` works straightforwardly.
    `pdftotext -layout` is not used because column widths differ per page and spaces
    inside the category column cannot be told apart from column boundaries.
    """
    import pdfplumber  # --group ingest

    header = None
    rows: list[dict] = []
    asof = ""
    with pdfplumber.open(str(pdf)) as doc:
        n_pages = len(doc.pages)
        for pi, page in enumerate(doc.pages, 1):
            if pi == 1:
                txt = page.extract_text() or ""
                m = re.search(r"（(令和\s*\d+\s*年\s*\d+\s*月)時点）", txt)
                asof = re.sub(r"\s+", "", m.group(1)) if m else ""
            for table in page.extract_tables():
                for r in table:
                    cells = [re.sub(r"\s+", " ", (c or "")).strip() for c in r]
                    if cells and cells[0] == "区分":
                        header = cells
                        continue
                    if len(cells) != 6:
                        sys.exit(f"touroku.pdf p.{pi}: row does not have 6 columns: {cells}")
                    if not any(cells):
                        continue
                    rows.append({
                        "kubun": cells[0], "name": cells[1], "person": cells[2],
                        "job": cells[3], "role": cells[4], "dept": cells[5], "page": pi,
                    })
    if header != ["区分", "施設基準名", "氏名", "職 種", "専任/専従", "診療科"]:
        sys.exit(f"unexpected header row in touroku.pdf: {header}")
    empty = [r for r in rows if not (r["kubun"] and r["name"] and r["person"])]
    if empty:
        sys.exit(f"touroku.pdf: {len(empty)} rows with an empty kubun / name / person: {empty[:3]}")
    return {"as_of": asof, "pages": n_pages, "columns": header, "rows": rows}


# ---------------------------------------------------------------------------


def _pdftotext(pdf: Path, txt: Path) -> None:
    """`pdftotext -layout`. The circulars also express hierarchy ((１) / ア / (イ)) by indentation, so -layout is required."""
    subprocess.run(["pdftotext", "-layout", str(pdf), str(txt)], check=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="only print the URLs")
    ap.add_argument("--skip-his", action="store_true", help="skip the intranet (his-info) side")
    args = ap.parse_args()

    CACHE.mkdir(parents=True, exist_ok=True)
    s = requests.Session()
    s.headers["User-Agent"] = UA
    manifest: dict = {"retrieved_at": dt.date.today().isoformat(), "sources": {}}

    print(f"[1] revision special page {KAITEI_URL}")
    kaitei = _get(s, KAITEI_URL)
    kaitei.encoding = kaitei.apparent_encoding or "utf-8"
    khtml = kaitei.text
    k_label, k_url, k_note = _pick(khtml, KIHON_LABEL)
    t_label, t_url, t_note = _pick(khtml, TOKKEI_LABEL)
    print(f"    kihon  {k_label} {k_note} -> {k_url}")
    print(f"    tokkei {t_label} {t_note} -> {t_url}")
    checklists = _checklists(khtml)
    for lab, url, date in checklists:
        print(f"    (reference) {lab} {date} -> {url}")

    if args.dry_run:
        if not args.skip_his:
            print(f"[2] his-info {HIS_TOP}")
            fetch_his(requests.Session(), dry=True)
        print("\n(dry-run: stopping here)")
        return

    (CACHE / "mhlw_kaitei.html").write_text(khtml, encoding="utf-8")

    def save(name: str, url: str, note: str, blob: bytes | None = None) -> None:
        if blob is None:
            blob = _get(s, url, binary=True)
        (CACHE / name).write_bytes(blob)
        manifest["sources"][name] = {
            "url": url, "bytes": len(blob), "sha256": _sha256(blob), "note": note,
        }
        print(f"    {name}: {len(blob):,} bytes")

    print("[2] facility-standard circular PDFs")
    save("kihon.pdf", k_url, f"{k_label} {k_note}".strip())
    save("tokkei.pdf", t_url, f"{t_label} {t_note}".strip())
    for i, (lab, url, date) in enumerate(checklists, 1):
        save(f"checklist_{i}.pdf", url, f"{lab} ({date}) — 参考。build では使わない")

    print("[3] pdftotext -layout")
    for stem in ("kihon", "tokkei"):
        _pdftotext(CACHE / f"{stem}.pdf", CACHE / f"{stem}.txt")
        print(f"    {stem}.txt: {(CACHE / f'{stem}.txt').stat().st_size:,} bytes")

    if not args.skip_his:
        print(f"[4] his-info {HIS_TOP} (intranet, must not leave the hospital)")
        hs = requests.Session()
        blob, url, top = fetch_his(hs, dry=False)
        assert blob is not None
        (CACHE / "his_top.html").write_text(top, encoding="utf-8")
        save("touroku.pdf", url, f"施設基準に関する職員の登録状況 (his-info トップの postback 経由)。"
             "**院内イントラ由来・院外持ち出し禁止**", blob)
        info = extract_touroku(CACHE / "touroku.pdf")
        (CACHE / "touroku.json").write_text(
            json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        manifest["touroku"] = {
            "as_of": info["as_of"], "pages": info["pages"], "rows": len(info["rows"]),
            "distinct_kijun": len({(r["kubun"], r["name"]) for r in info["rows"]}),
        }
        print(f"    touroku.json: {len(info['rows'])} rows / {info['pages']} pages / "
              f"as of {info['as_of']} / {manifest['touroku']['distinct_kijun']} distinct facility standards")

    (CACHE / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("\nwrote manifest.json")


if __name__ == "__main__":
    main()
