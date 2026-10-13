"""CTCAE v5.0 - JCOG Japanese translation PDF → per-SOC intermediate JSON + .md generation.

Extracts tables from the PDF (cache/CTCAEv5J_20250901_v28_1.pdf) with pdfplumber,
detects SOC (System Organ Class) boundaries and splits per SOC.

Output:
  - cache/extracts/CTCAEv5_JCOG_2025/<soc_slug>.json (intermediate representation)
  - with --emit-md, writes data/doc/診療ガイドライン/CTCAEv5_JCOG_2025/<SOC>.md directly

CTCAE carries little information per AE (1-2 sentences for Grade 1-5), so entries are
aggregated per SOC rather than per AE: 1 SOC = 1 .md.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import pdfplumber

ROOT = Path(__file__).resolve().parent.parent
PDF_PATH = ROOT / "cache" / "CTCAEv5J_20250901_v28_1.pdf"
DOMAIN_ID = "CTCAEv5_JCOG_2025"
EXTRACTS_DIR = ROOT / "cache" / "extracts" / DOMAIN_ID
DATA_DIR = ROOT / "data" / "doc" / "診療ガイドライン" / DOMAIN_ID

# Parsing SOC band rows. Example: "血液およびリンパ系障害 Blood and lymphatic system disorders"
# Newlines are collapsed to spaces before splitting the Japanese and English names.
SOC_BAND_RE = re.compile(r"^(.+?)\s+([A-Za-z][A-Za-z\s,&\-/()（）]+)$")

# Labels at which an in-cell newline is kept as an item separator.
# Cases like the hypertension grade column, where criteria for several populations line up as "成人:" "小児:" "青年:".
INLINE_LABELS = ("成人および小児", "小児および青年", "成人", "小児", "青年", "新生児")


def normalize_cell(text: str) -> str:
    """Normalize cell text.

    - Newlines generally come from PDF word-wrap → replaced with a space
    - But a newline directly followed by one of INLINE_LABELS is a meaningful separator → kept
    """
    if not text:
        return ""
    # Protect labels like 「成人:」: newline + label + colon → temporary special marker
    s = text
    # Label protection: newline + INLINE_LABELS + colon → keep the meaningful newline via a temporary marker
    for label in INLINE_LABELS:
        s = re.sub(
            rf"\n\s*({re.escape(label)})\s*[::]",
            lambda m: "\x00" + m.group(1) + ": ",
            s,
        )
    # A newline right after a hyphen joins without a space (e.g. "LLN-\n10.0" → "LLN-10.0", "120-\n139" → "120-139")
    s = re.sub(r"([\-‐‑‒–—])\n\s*", r"\1", s)
    # Remaining newlines become spaces (PDF word-wrap)
    s = s.replace("\n", " ")
    # Collapse runs of whitespace to one
    s = re.sub(r"[ \t]+", " ", s).strip()
    # Remove spaces between CJK characters (reclaim word-wrap spaces inserted mid-Japanese)
    cjk = r"[　-〿぀-ゟ゠-ヿ一-鿿＀-￯]"
    while True:
        new_s = re.sub(rf"({cjk}) +({cjk})", r"\1\2", s)
        if new_s == s:
            break
        s = new_s
    # Restore the protected meaningful newlines
    s = s.replace("\x00", "\n")
    return s


def parse_pdf(pdf_path: Path) -> dict[str, dict]:
    """Extract tables from the PDF → return a per-SOC dict.

    Returns:
      {
        "<SOC Japanese name>": {
          "soc_en": "<English name>",
          "entries": [
            {"medra_code": str, "soc_ja": str, "term_en": str, "term_ja": str,
             "grades": [str|None]*5, "definition": str, "nav_note": str},
            ...
          ]
        },
        ...
      }
    """
    soc_data: dict[str, dict] = {}
    soc_order: list[str] = []
    current_soc: str | None = None

    with pdfplumber.open(pdf_path) as pdf:
        # The table body starts at p7 (page 1/46). p1-p6 are the preface.
        for page in pdf.pages[6:]:
            for tbl in page.extract_tables():
                for row in tbl:
                    if not row or not any(row):
                        continue
                    first = (row[0] or "").strip()
                    # Skip header rows (column titles)
                    if "MedDRA" in first or "CTCAE" in first:
                        continue
                    rest_empty = all(not (c or "").strip() for c in row[1:])
                    # SOC band row: text only in column 1, Japanese and English side by side
                    if rest_empty and first and re.search(r"[A-Za-z]", first):
                        merged = first.replace("\n", " ")
                        m = SOC_BAND_RE.match(merged)
                        if m:
                            soc_ja = m.group(1).strip()
                            soc_en = m.group(2).strip()
                        else:
                            soc_ja, soc_en = merged.strip(), ""
                        if soc_ja not in soc_data:
                            soc_data[soc_ja] = {"soc_en": soc_en, "entries": []}
                            soc_order.append(soc_ja)
                        current_soc = soc_ja
                        continue
                    # Data row: column 1 is the MedDRA Code (8 digits)
                    if first.isdigit() and current_soc:
                        grades_raw = [(row[i] or "").strip() for i in range(4, 9)]
                        grades = [normalize_cell(g) for g in grades_raw]
                        # "-" or empty string becomes None
                        grades = [None if g in ("", "-") else g for g in grades]
                        definition_raw = (row[9] or "").strip()
                        definition = normalize_cell(definition_raw)
                        if definition in ("", "-"):
                            definition = None
                        nav_note_raw = (row[10] or "").strip() if len(row) > 10 else ""
                        nav_note = normalize_cell(nav_note_raw)
                        if nav_note in ("", "-"):
                            nav_note = None
                        soc_data[current_soc]["entries"].append(
                            {
                                "medra_code": first,
                                "soc_ja": normalize_cell((row[1] or "").strip()),
                                "term_en": normalize_cell((row[2] or "").strip()),
                                "term_ja": normalize_cell((row[3] or "").strip()),
                                "grades": grades,
                                "definition": definition,
                                "nav_note": nav_note,
                            }
                        )
    # Preserve SOC order of appearance (≈ alphabetical)
    return {k: soc_data[k] for k in soc_order}


def soc_slug(soc_ja: str) -> str:
    """Use the Japanese SOC name as is for the JSON output file name (assumes a UTF-8 capable FS)."""
    return soc_ja


def write_json(soc_data: dict[str, dict]) -> None:
    EXTRACTS_DIR.mkdir(parents=True, exist_ok=True)
    for soc_ja, data in soc_data.items():
        out = EXTRACTS_DIR / f"{soc_slug(soc_ja)}.json"
        out.write_text(
            json.dumps({"soc_ja": soc_ja, **data}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"  wrote {out.relative_to(ROOT)} ({len(data['entries'])} entries)")


def render_grade_block(grade_text: str) -> str:
    """Render a Grade cell body as MD.

    If inner-newline labels like "成人:" "小児:" remain, expand into a nested list.
    """
    if "\n" not in grade_text:
        return grade_text
    # Turn label lines into list items
    lines = grade_text.split("\n")
    out_lines = []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        out_lines.append(ln)
    if len(out_lines) == 1:
        return out_lines[0]
    # Nested list form
    return "\n" + "\n".join(f"    - {ln}" for ln in out_lines)


def render_md_for_soc(soc_ja: str, data: dict) -> str:
    """Format a whole SOC as one Markdown document.

    - front matter first
    - each AE is a `## <n>. <Japanese term> (<English term>)` heading
    - MedDRA Codes go in the front matter meddra_codes (a list in the same order as the AEs).
      Not in the body: the code is provenance, neither answer content nor a query key, and
      putting it in the body (the single source for grep/read) causes grep/read asymmetry and
      OWUI markdown breakage from raw HTML comments (docs/cage/07). `?` hidden keeps it out of list_doc.
      Body `##` order == meddra_codes index order is guaranteed by generation (guards the positional fragility).
    - Grades are a `- **Grade N**: ...` list; "-" grades are omitted
    - definition is `**定義**: ...`
    - navigational note, when present, is `**検索上の注意**: ...`
    - AEs are separated by `---`
    """
    soc_en = data["soc_en"]
    entries = data["entries"]
    # Front matter is title + entry_count + meddra_codes. soc/soc_en duplicate the title, so
    # they are omitted. entry_count / meddra_codes? are governed by the CTCAE node's
    # fields: {entry_count: int, meddra_codes?: list} (meddra_codes is `?` hidden).
    codes_yaml = "[" + ", ".join(str(e["medra_code"]) for e in entries) + "]"
    fm = (
        "---\n"
        f"title: {soc_ja} ({soc_en})\n"
        f"entry_count: {len(entries)}\n"
        f"meddra_codes: {codes_yaml}\n"
        "---\n\n"
    )
    intro = (
        f"CTCAE v5.0 (JCOG 日本語訳, MedDRA/J v28.1 対応, 2025-09-01) の "
        f"SOC 「{soc_ja}」配下に属する有害事象用語 全 {len(entries)} 件。"
        f"配列順は CTCAE 原典のアルファベット順 (英語 Term 基準)。\n\n"
        f"Grade 1〜5 の一般定義・ADL・nearest match 原則・略語一覧などは "
        f"ドメイン直下 `_index.md` に集約してある。"
    )
    parts = [fm, intro, "\n"]
    for i, e in enumerate(entries, start=1):
        parts.append("\n---\n\n")
        parts.append(f"## {i}. {e['term_ja']} ({e['term_en']})\n\n")
        for n, g in enumerate(e["grades"], start=1):
            if g is None:
                continue
            parts.append(f"- **Grade {n}**: {render_grade_block(g)}\n")
        if e["definition"]:
            parts.append(f"\n**定義**: {e['definition']}\n")
        if e["nav_note"]:
            parts.append(f"\n**検索上の注意**: {e['nav_note']}\n")
    return "".join(parts)


def write_md(soc_data: dict[str, dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for soc_ja, data in soc_data.items():
        out = DATA_DIR / f"{soc_slug(soc_ja)}.md"
        out.write_text(render_md_for_soc(soc_ja, data), encoding="utf-8")
        size_kb = out.stat().st_size / 1024
        print(
            f"  wrote {out.relative_to(ROOT)} "
            f"({len(data['entries'])} entries, {size_kb:.1f} KB)"
        )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--emit-md", action="store_true", help="write .md files directly under data/doc/")
    ap.add_argument(
        "--json-only", action="store_true", help="write only JSON to cache/extracts (no .md)"
    )
    args = ap.parse_args(argv)

    if not PDF_PATH.exists():
        print(f"ERROR: PDF not found: {PDF_PATH}", file=sys.stderr)
        return 1

    print(f"parsing {PDF_PATH.relative_to(ROOT)} ...")
    soc_data = parse_pdf(PDF_PATH)
    total = sum(len(d["entries"]) for d in soc_data.values())
    print(f"  {len(soc_data)} SOCs, {total} entries")

    print(f"\nwriting JSON to {EXTRACTS_DIR.relative_to(ROOT)}/ ...")
    write_json(soc_data)

    if args.emit_md and not args.json_only:
        print(f"\nwriting MD to {DATA_DIR.relative_to(ROOT)}/ ...")
        write_md(soc_data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
