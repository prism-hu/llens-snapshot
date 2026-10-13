#!/usr/bin/env python3
"""Convert the shisetsu kijun (facility standard) circulars + our hospital's registrant list into the doc tree.

Input (cache/shisetsu_kijun/, output of `fetch_shisetsu_kijun.py`):
  - kihon.txt   … 基本診療料の施設基準等及びその届出に関する手続きの取扱いについて (保医発0305第7号)
  - tokkei.txt  … 特掲診療料の施設基準等及びその届出に関する手続きの取扱いについて (保医発0305第8号)
  - touroku.json … table of the in-house 「施設基準登録者一覧」 (registrant list; Medical Affairs Division, as of July 2026)
  - manifest.json

Output (**split into 2 categories because of the export restriction**):
  - data/doc/診療報酬/施設基準R08_基本診療料/**     ← circular (PDL1.0, may leave the hospital)
  - data/doc/診療報酬/施設基準R08_特掲診療料/**     ← same
  - data/doc/院内マニュアル/施設基準届出状況/**      ← registrant list (from the hospital intranet, must not leave)

**1 node = one 「第N」 of the circular = 1 facility standard.** The circular lays out facility standards
under headings `第１`, `第１の２`, `第 13 の２の２`, and that granularity matches 「急性期総合体制加算の施設基準」
or 「外来腫瘍化学療法診療料の施設基準」. `削除` (deleted) headings do not become nodes (no body).

## Hierarchy — directories by circular attachment / fee schedule part

  基本診療料 (133 headings): attachments 1〜5 are exactly the 5 divisions 「初・再診料 / 入院基本料等 / 入院基本料等加算 /
  特定入院料 / 短期滞在手術等基本料」, so attachment = directory (at most 75 children).
  特掲診療料 (541 headings): attachment 1 lists 第1〜第109 **flat, 537 of them**. The circular itself has no divisions, so
  **cut by fee schedule part (B 医学管理等 … O その他)**. The circular order follows the notification (特掲診療料の施設基準等)
  = nearly the fee schedule part order, so part boundaries are decided by the 「第N」 number (`TOKKEI_PARTS`; only 第9
  在宅療養支援診療所, which is out of order, is in `PART_OVERRIDE`). verify checks the boundaries against "the part of the
  nodes whose kubun number was resolved by name matching" (the first draft of the boundaries was off by 23 and was fixed by verify).
  Dental-only (歯冠修復・歯科矯正) and pharmacy have no part in the medical fee schedule, so they go in `歯科M_…` / `歯科N_…` / `調剤`.
  **K 手術 has 211**, so it is further cut by fee schedule subsection (`K_SUBDIRS`). The subsection is taken from the path of the
  fee schedule node matched by name; unmatched headings inherit the subsection of the previous heading (the order is by subsection).
  Only the general rules (通則) and dental fee schedule surgeries are decided by number. K on the registrant list side goes in the same subsection directories.

  The opening 「第１ 施設基準等 / 第２ 届出に関する手続き / 第３ 届出受理後の措置等 / 第４ 経過措置等」 of the circular
  is not an attachment but **the common notification rules (acceptance number, track record period, transitional measures)**, so it goes in `00_通知本文/`.

## Large 「第N」 are split into child nodes

  A 第N exceeding `BLOCK_SPLIT`, like `第２ 病院の入院基本料等に関する施設基準` (47,688 chars), is split into
  child nodes at its `１`, `２`, … items (Arabic-numbered items at line start).
  The parent (`_index.md`) holds the preamble and the list of children; the children name the parent's path. **Same rule as
  the fee schedule's "large blocks go to child nodes"** (more nodes, but still 1 node 1 file).

## Matching against the fee schedule and the registrant list (paths named in both directions)

  Facility standard node → fee schedule node: match the heading name against the fee schedule node title (exact match →
  match with parentheses dropped → definition in the notification text `<名称>として` / item line). Headings that refer to the
  general rules (`医科点数表第２章第 10 部手術の通則の５`) name the part's `_index`. **Existing fee schedule nodes are not touched**.
  Facility standard node ⇄ registrant list node: match by name (prefix) and kubun number, and **write the other's path on both
  sides**. Whether a notification was filed is not copied into `/診療報酬` (only the path is named).

usage: uv run python scripts/build_shisetsu_kijun.py   (run from inside cage/)
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from textfix import doc_ref, safe_segment  # noqa: E402

CACHE = Path("cache/shisetsu_kijun")
CATEGORY = Path("data/doc/診療報酬")
MANUAL = Path("data/doc/院内マニュアル")

SEC_KIHON = "施設基準R08_基本診療料"
SEC_TOKKEI = "施設基準R08_特掲診療料"
SEC_TOUROKU = "施設基準届出状況"  # under 院内マニュアル
TENSU_CH1 = "医科点数表R08_基本診療料"
TENSU_CH2 = "医科点数表R08_特掲診療料"
# Q&A (separate section, up to その12 = 2026-09-02. Same basename as the fee schedule per kubun; questions with no kubun under `区分番号なし/`)
GIGI = "疑義解釈R08"
GIGI_AS_OF = "その12 (2026-09-02 発出) まで"

BLOCK_SPLIT = 20000  # a 「第N」 above this many chars is split into child nodes at its `１`,`２`… items
CHILD_LIMIT = 80  # guideline for children per directory (not split beyond it if the notification has no division)

PUBLISHER = "厚生労働省"
APPLIES_FROM = "2026-06-01"
SOURCE = "https://www.mhlw.go.jp/stf/newpage_67729.html"
HIS_SOURCE = "https://<redacted-intranet-host>/his-info/top.aspx"  # fetch source (<redacted-net>)
# Do not bake the his-info origin into the body / front matter (it differs between <redacted-net> and <redacted-net>). Write `{{HIS_FAQ_BASE}}`
# as-is, and the cage build substitutes it from env CAGE_HIS_BASE (DEPLOY_PLACEHOLDERS in cage/cage/build.py).
HIS_FETCHED_BASE = "https://<redacted-intranet-host>/his-info"


# Paths of other nodes written in the body use the canonical form `{{DOC_BASE}}/…/<name>.md` (leaf) / `{{DOC_BASE}}/…/` (directory)
# (textfix.doc_ref). Internally (index keys, matching) keep `/<category>/…` without `.md`, and convert only
# when writing out. For nodes we write ourselves the kind is known, so state it; for nodes of existing trees
# (fee schedule, Q&A) decide by what exists on disk (directory or .md).
def tree_ref(path: str) -> str:
    """Path of a node in an existing tree → canonical form for the body (leaf or directory decided by what exists under data/doc)."""
    return doc_ref(path, is_dir=(Path("data/doc") / path.lstrip("/")).is_dir())


def dir_ref(path: str) -> str:
    return doc_ref(path, is_dir=True)


def leaf_ref(path: str) -> str:
    return doc_ref(path, is_dir=False)


def his_doc_url(url: str) -> str:
    """Fetched his-info URL → the form written in the body / front matter (`{{HIS_FAQ_BASE}}/…`)."""
    return url.replace(HIS_FETCHED_BASE, "{{HIS_FAQ_BASE}}", 1)


TOUROKU_VERSION = "202607"  # as-of date of the registrant list (July 2026)
TOUROKU_CONTACT = "医事課施設基準係"

NOTICES = {
    "kihon": {
        "section": SEC_KIHON,
        "short": "基本診療料の施設基準",
        "title": "基本診療料の施設基準等及びその届出に関する手続きの取扱いについて",
        "number": "令和8年3月5日 保医発0305第7号",
        "pdf": "kihon.pdf",
        "tensu": TENSU_CH1,
        "chapter": "医科点数表 第1章 基本診療料",
        # attachment number → (source title (checked by the build), directory name). **Forms (attachment 6 onward) are not ingested**
        "betten": {
            "1": ("初・再診料の施設基準等", "01_初・再診料"),
            "2": ("入院基本料等の施設基準等", "02_入院基本料等"),
            "3": ("入院基本料等加算の施設基準等", "03_入院基本料等加算"),
            "4": ("特定入院料の施設基準等", "04_特定入院料"),
            "5": ("短期滞在手術等基本料の施設基準等", "05_短期滞在手術等基本料"),
        },
    },
    "tokkei": {
        "section": SEC_TOKKEI,
        "short": "特掲診療料の施設基準",
        "title": "特掲診療料の施設基準等及びその届出に関する手続きの取扱いについて",
        "number": "令和8年3月5日 保医発0305第8号",
        "pdf": "tokkei.pdf",
        "tensu": TENSU_CH2,
        "chapter": "医科点数表 第2章 特掲診療料",
        "betten": {"1": ("特掲診療料の施設基準等", None)},  # None = cut by part (TOKKEI_PARTS)
    },
}
HONBUN_DIR = "00_通知本文"

# 「第N」 of 特掲診療料 attachment 1 → fee schedule part. From the **boundary 第N** up to before the next boundary is that part.
# The circular follows the order of the notification 「特掲診療料の施設基準等」 (= fee schedule part order), so the number decides.
# Dental-only and pharmacy have no part in the medical fee schedule. verify checks against the name-matching results.
TOKKEI_PARTS: list[tuple[tuple[int, ...], str, str]] = [
    ((1,), "B_医学管理等", "第1部 医学管理等"),
    # Only 第9 在宅療養支援診療所 sits inside the 医学管理等 run (PART_OVERRIDE below). 第10〜13の4 are
    # ハイリスク妊産婦共同管理料 (B005-4) … dental medical management fees; 在宅医療 starts at 第14
    ((14,), "C_在宅医療", "第2部 在宅医療"),
    ((18,), "D_検査", "第3部 検査"),
    ((30,), "E_画像診断", "第4部 画像診断"),
    ((36, 2), "F_投薬", "第5部 投薬"),
    ((37,), "G_注射", "第6部 注射"),
    ((38,), "H_リハビリテーション", "第7部 リハビリテーション"),
    ((47, 6), "I_精神科専門療法", "第8部 精神科専門療法"),  # from 第47の6 経頭蓋磁気刺激療法 (I000-2)
    ((56, 2), "J_処置", "第9部 処置"),
    ((57, 5), "歯科M_歯冠修復及び欠損補綴", "歯科点数表 第12部 歯冠修復及び欠損補綴"),
    ((57, 8), "K_手術", "第10部 手術"),
    ((81,), "L_麻酔", "第11部 麻酔"),
    ((82,), "M_放射線治療", "第12部 放射線治療"),
    ((84, 3), "N_病理診断", "第13部 病理診断"),
    ((85,), "歯科M_歯冠修復及び欠損補綴", "歯科点数表 第12部 歯冠修復及び欠損補綴"),
    ((86,), "歯科N_歯科矯正", "歯科点数表 第13部 歯科矯正"),
    ((88,), "調剤", "調剤報酬点数表"),
    ((104,), "O_その他", "第14部 その他"),
]
# Headings that do not follow the number boundaries (where the circular order departs from the fee schedule part order)
PART_OVERRIDE: dict[tuple[int, ...], tuple[str, str]] = {
    (9,): ("C_在宅医療", "第2部 在宅医療"),  # 在宅療養支援診療所 (sits between 第8 開放型病院共同指導料 and 第11)
}
PART_LETTER_BY_NUM = dict(zip(range(1, 15), "BCDEFGHIJKLMNO"))  # 第N部 of chapter 2 → letter
PART_NAME = {
    "A": "基本診療料", "B": "医学管理等", "C": "在宅医療", "D": "検査", "E": "画像診断",
    "F": "投薬", "G": "注射", "H": "リハビリテーション", "I": "精神科専門療法", "J": "処置",
    "K": "手術", "L": "麻酔", "M": "放射線治療", "N": "病理診断", "O": "その他",
}

# ---------------------------------------------------------------------------
# Regular expressions
# ---------------------------------------------------------------------------

_Z2H = str.maketrans(
    "".join(chr(0xFF10 + i) for i in range(10)) + "".join(chr(0xFF21 + i) for i in range(26)),
    "0123456789" + "".join(chr(65 + i) for i in range(26)),
)
# 「第N」「第N のN」「第 13 の２の２」. **At line start (a page-break \x0c is allowed)**. Wrapped body text
# can start a line as `第 71 号）が…`, so if the heading word is followed by 条/号/項/章/部/節/款 it is
# not a heading. **Do not reject hiragana-initial titles** — 13 real ones such as `がん性疼痛緩和指導管理料`
# `てんかん指導料` `こころの連携指導料` `せん妄ハイリスク患者ケア加算`
HEADING_RE = re.compile(
    r"^\x0c?第[\s　]*([０-９0-9]{1,3})((?:[\s　]*の[\s　]*[０-９0-9]{1,2})*)[\s　]+(\S.*)$"
)
HEADING_REJECT = re.compile(r"^[条号項章部節款]")
BETTEN_RE = re.compile(r"^\x0c?別添[\s　]*([０-９0-9]{1,2})((?:[\s　]*の[\s　]*[０-９0-9])?)\s*$")
# Items inside a 第N (`１ …` / `11 の２ …`). Indented 0〜2 chars. If the numbers are not consecutive it is a wrapped line
SUB_RE = re.compile(r"^\x0c?[\s　]{0,2}([０-９0-9]{1,2})((?:[\s　]*の[\s　]*[０-９0-9])?)[\s　]+(\S.*)$")
FOOTER_RE = re.compile(r"^\s*-\s*\d+\s*-\s*$")
# Headings referring to the general rules (`医科点数表第２章第 10 部手術の通則の５`) → _index of the fee schedule part
TSUSOKU_REF_RE = re.compile(r"医科点数表第[２2]章第[\s　]*([０-９0-9]{1,2})[\s　]*部")


def _int(s: str) -> int:
    return int(s.translate(_Z2H))


def heading_key(num: str, subs: str) -> tuple[int, ...]:
    """`('13', ' の２の２')` → (13, 2, 2)."""
    return (_int(num),) + tuple(_int(x) for x in re.findall(r"[０-９0-9]+", subs))


def key_label(key: tuple[int, ...]) -> str:
    """(13, 2, 2) → `第13の2の2` (for front matter / body)."""
    return "第" + "の".join(str(k) for k in key)


def key_fname(key: tuple[int, ...]) -> str:
    """(13, 2, 2) → `第013の02の02` (**zero-padded**: the walker sorts by name, so without padding
    第10 would come before 第2)."""
    return "第" + f"{key[0]:03d}" + "".join(f"の{k:02d}" for k in key[1:])


_FS_BAD = re.compile(r'[/\\:*?"<>|\x00-\x1f]')


def fs_safe(s: str, limit: int = 50) -> str:
    s = _FS_BAD.sub("・", re.sub(r"[\s　]+", "", s))
    return safe_segment(s[:limit])  # URL-breaking ASCII → full-width (textfix.safe_segment)


def yaml_str(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


# Glyph variants that differ between the circular and the notification (circular `後縦靭帯骨化症手術` / notification `後縦靱帯骨化症手術`)
_VARIANTS = str.maketrans({"靭": "靱", "頚": "頸", "弯": "彎", "鼡": "鼠", "藪": "薮"})


def norm(s: str) -> str:
    """For matching: NFKC + whitespace removal + glyph variant folding."""
    return re.sub(r"[\s　]", "", unicodedata.normalize("NFKC", s)).translate(_VARIANTS)


def strip_paren(s: str) -> str:
    """Drop the trailing parenthetical (including nesting): `緑内障手術(緑内障治療用インプラント挿入術(プレートのあるもの))`
    → `緑内障手術`. **Counts matching parentheses** — peeling from the outside with a regex stops at the nested tail `))`."""
    while s and s[-1] in ")）":
        depth = 0
        for i in range(len(s) - 1, -1, -1):
            if s[i] in ")）":
                depth += 1
            elif s[i] in "(（":
                depth -= 1
                if depth == 0:
                    s = s[:i].rstrip()
                    break
        else:
            return s  # unmatched parenthesis
    return s


# ---------------------------------------------------------------------------
# Slicing the circular
# ---------------------------------------------------------------------------


def clean(lines: list[str]) -> str:
    """Drop page footers / page breaks and remove the common indent.

    **Ruby lines are not dropped.** Unlike the fee schedule, in the circular's `-layout` output a
    "line of only 1〜4 hiragana indented 4+ chars" is a wrap fragment (`…につい` / `て`), and dropping it
    would lose body text (measured: 46 such lines in 基本診療料, 11 in 特掲, 0 ruby).
    """
    out = []
    for line in lines:
        line = line.replace("\x0c", "")
        if FOOTER_RE.match(line):
            continue
        out.append(line.rstrip())
    while out and not out[0].strip():
        out.pop(0)
    while out and not out[-1].strip():
        out.pop()
    body = [ln for ln in out if ln.strip()]
    if not body:
        return ""
    indent = min(len(ln) - len(ln.lstrip()) for ln in body)
    out = [ln[indent:] if ln.strip() else "" for ln in out]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out))


def page_of(lines: list[str], i: int, page_offsets: list[int]) -> int:
    """Line number → physical PDF page (cumulative page breaks \x0c)."""
    import bisect
    return bisect.bisect_right(page_offsets, i) + 1


def split_notice(name: str) -> dict:
    """Cut the circular text into main text + attachments and return the 「第N」 blocks of each.

    Returns: {"honbun": [block…], "betten": {"1": {"title": str, "preamble": [lines], "blocks": [block…]}}}
    block = {"key": (int…), "title": str, "lines": [str], "line_no": int, "page": int, "deleted": bool}
    """
    conf = NOTICES[name]
    lines = (CACHE / f"{name}.txt").read_text(encoding="utf-8").split("\n")
    page_offsets = [i for i, ln in enumerate(lines) if ln.startswith("\x0c")]

    wanted = set(conf["betten"])
    where = "honbun"  # or attachment number / "skip"
    honbun: list[dict] = []
    betten: dict[str, dict] = {}
    cur: dict | None = None
    preamble_of: list[str] | None = None
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        m = BETTEN_RE.match(line)
        if m:
            cur = None
            num = str(_int(m.group(1))) + (
                f"の{_int(re.sub(r'[^０-９0-9]', '', m.group(2)))}" if m.group(2).strip() else ""
            )
            if num in wanted:
                where = num
                # Check the title line (first non-blank line after the attachment marker) against the source — so a
                # revision that reorders the attachments does not silently ingest another attachment as 「入院基本料等」
                j = i + 1
                while j < n and not lines[j].strip():
                    j += 1
                got = norm(lines[j]) if j < n else ""
                exp = norm(conf["betten"][num][0])
                if got != exp:
                    sys.exit(f"{name}: unexpected title for 別添{num}: {lines[j].strip()!r} (expected {conf['betten'][num][0]!r})")
                betten[num] = {"title": conf["betten"][num][0], "preamble": [], "blocks": []}
                preamble_of = betten[num]["preamble"]
                i = j + 1
                continue
            where = "skip"
            preamble_of = None
            i += 1
            continue
        if where == "skip":
            i += 1
            continue
        m = HEADING_RE.match(line)
        if m and not HEADING_REJECT.match(m.group(3)):
            key = heading_key(m.group(1), m.group(2))
            title = re.sub(r"[\s　]+", " ", m.group(3)).strip()
            # A wrapped heading → join. **The continuation is indented 0〜4 chars** and is neither an
            # item (`１ …`), a numbered parenthesis (`(１)`), nor the next heading. The first body line
            # is indented 6+ chars, so it does not mix in. Some headings span 3 lines (別添2 第1), so
            # repeat while the previous line is full width (44+ chars)
            head_lines = [line]
            last = line
            j = i + 1
            while len(last.rstrip()) >= 44:
                while j < n and not lines[j].strip():
                    j += 1
                if (
                    j < n
                    and not SUB_RE.match(lines[j])
                    and not re.match(r"^[\s　]*[（(][０-９0-9イロハニ]", lines[j])
                    and not HEADING_RE.match(lines[j])
                    and not BETTEN_RE.match(lines[j])
                    and len(lines[j]) - len(lines[j].lstrip(" 　\x0c")) <= 4
                ):
                    title += lines[j].strip()
                    head_lines.append(lines[j])
                    last = lines[j]
                    i = j  # the continuation line is treated as part of the heading on the body side
                    j += 1
                else:
                    break
            cur = {
                "key": key, "title": title, "lines": head_lines,
                "line_no": i + 1, "page": page_of(lines, i, page_offsets),
                # Forms like `削除` / `第 24 から第 24 の４まで 削除`. No body, so not a node
                "deleted": bool(re.search(r"削除$", title)),
            }
            (honbun if where == "honbun" else betten[where]["blocks"]).append(cur)
            i += 1
            continue
        if cur is not None:
            cur["lines"].append(line)
        elif preamble_of is not None:
            preamble_of.append(line)
        i += 1
    # If the headings are not monotonic the circular structure differs from the assumption (part boundaries are decided by number)
    for num, b in betten.items():
        keys = [x["key"] for x in b["blocks"]]
        for a, c in zip(keys, keys[1:]):
            if c <= a:
                sys.exit(f"{name}: heading numbers go backwards in 別添{num}: {key_label(a)} → {key_label(c)}")
    missing = wanted - set(betten)
    if missing:
        sys.exit(f"{name}: 別添 {sorted(missing)} not found")
    return {"honbun": honbun, "betten": betten, "lines": lines}


def split_subs(block: dict) -> list[dict] | None:
    """Split a large 「第N」 at line-start items (`１`, `２`, `11 の２`…). Non-consecutive numbers are treated as wrapped lines."""
    lines = block["lines"]
    subs: list[dict] = []
    expect = 1
    head: list[str] = [lines[0]]
    cur: dict | None = None
    for ln in lines[1:]:
        m = SUB_RE.match(ln)
        ok = False
        if m:
            num = _int(m.group(1))
            sub2 = m.group(2).strip()
            if (num == expect and not sub2) or (cur is not None and num == expect - 1 and sub2):
                ok = True
                if not sub2:
                    expect += 1
        if ok:
            assert m is not None
            label = str(_int(m.group(1))) + (f"の{_int(x)}" if (x := re.sub(r'\D', '', m.group(2))) else "")
            cur = {"label": label, "title": re.sub(r"[\s　]+", " ", m.group(3)).strip(), "lines": [ln]}
            subs.append(cur)
        elif cur is not None:
            cur["lines"].append(ln)
        else:
            head.append(ln)
    if len(subs) < 2:
        return None
    block["head_lines"] = head
    return subs


def sub_title(t: str, limit: int = 40) -> str:
    """Cut the heading-like part out of an item's first line (back up to punctuation instead of mid-sentence).

    The first line of an item is often a sentence like `４ 入院患者の数及び看護要員の数等については下記のとおりとする。`.
    Drop the predicate (`については…` `は、次の…`) and use only the topic as the name.
    """
    t = re.sub(r"[\s　]+", "", t)
    cut = re.sub(r"(については|は、|は次の|とは、|とは|として、|に関する事項|について、|について).*$", "", t)
    if len(cut) >= 4:
        t = cut
    if len(t) > limit:
        cut = max(t.rfind(c, 0, limit) for c in "、。（(」")
        t = t[:cut] if cut > 8 else t[:limit]
    return t.strip("、。（(」 ") or "項"


# ---------------------------------------------------------------------------
# Index of fee schedule nodes (existing nodes are read only, never touched)
# ---------------------------------------------------------------------------


def _frontmatter(text: str) -> dict[str, str]:
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    fm: dict[str, str] = {}
    if not m:
        return fm
    for line in m.group(1).split("\n"):
        km = re.match(r"^([\w?]+): (.*)$", line)
        if km:
            v = km.group(2).strip()
            if v.startswith("'") and v.endswith("'"):
                v = v[1:-1].replace("''", "'")
            fm[km.group(1)] = v
    return fm


def _kokuji_text(body: str) -> str:
    m = re.search(r"^## 告示 \(点数と注\)\n(.*?)(?=\n## |\Z)", body, re.S | re.M)
    if not m:
        return ""
    f = re.search(r"```\n(.*?)\n```", m.group(1), re.S)
    return f.group(1) if f else ""


def _kokuji_lines(text: str) -> tuple[str, list[str]]:
    """(full text joined with whitespace removed, list normalised per line)."""
    lines = [norm(ln) for ln in text.split("\n") if ln.strip()]
    return "".join(lines), lines


class TensuIndex:
    """Index of fee schedule nodes: (kubun number, title, path, notification text)."""

    def __init__(self) -> None:
        self.by_kubun: dict[str, str] = {}  # 'A200' → path
        self.by_name: dict[str, list[tuple[str, str]]] = defaultdict(list)  # norm(title) → [(kubun, path)]
        self.kokuji: list[tuple[str, str, str]] = []  # (kubun, path, norm(notification))
        self.part_dir: dict[str, str] = {}  # 'K' → path of the part _index
        self.gigi_by_kubun: dict[str, str] = {}  # 'A205-2' → path of the Q&A node (only when it exists)
        groot = CATEGORY / GIGI
        if groot.is_dir():
            for p in groot.rglob("*.md"):
                if p.name == "_index.md":
                    continue
                fm = _frontmatter(p.read_text(encoding="utf-8"))
                k = fm.get("kubun", "")
                if re.match(r"^[A-Z]\d{3}", k) and "〜" not in k:
                    self.gigi_by_kubun[k] = "/" + str(p.relative_to("data/doc")).removesuffix(".md")
        for sec in (TENSU_CH1, TENSU_CH2):
            root = CATEGORY / sec
            if not root.is_dir():
                sys.exit(f"{root} not found — run build_shinryo_hoshu.py first")
            for p in root.rglob("*.md"):
                if p.parent == root and p.name == "_index.md":
                    continue
                text = p.read_text(encoding="utf-8")
                fm = _frontmatter(text)
                rel = "/" + str(p.relative_to("data/doc")).removesuffix(".md").removesuffix("/_index")
                kub = fm.get("kubun", "")
                if p.name == "_index.md" and p.parent.parent == root and sec == TENSU_CH2:
                    letter = p.parent.name[0]
                    self.part_dir[letter] = rel  # part directory (`K_手術`)
                    # General rules of the part (add-ons belonging to no kubun, such as `画像診断管理加算` `周術期栄養管理実施加算`)
                    self.kokuji.append((f"{letter}通則", rel, _kokuji_lines(text.split("\n---\n", 2)[-1])))
                if not re.match(r"^[A-Z]\d{3}", kub) or "〜" in kub:
                    continue
                if rel.endswith(("/告示", "/留意事項通知", "/コード表")):
                    if rel.endswith("/告示"):
                        self.kokuji.append((kub, rel.rsplit("/", 1)[0], _kokuji_lines(_kokuji_text(text))))
                    continue
                self.by_kubun[kub] = rel
                title = fm.get("title", "")
                # Exact match → form with parentheses dropped → each element of a juxtaposition like
                # `黄斑局所網膜電図、全視野精密網膜電図` (the circular writes `Ａ及びＢ`, the notification `Ａ、Ｂ`)
                keys = [norm(title), norm(strip_paren(title))]
                keys += [norm(strip_paren(x)) for x in re.split(r"[、,]|及び", strip_paren(title)) if len(x) >= 3]
                for k in dict.fromkeys(keys):
                    if k:
                        self.by_name[k].append((kub, rel))
                kt = _kokuji_text(text)
                if kt:
                    self.kokuji.append((kub, rel, _kokuji_lines(kt)))

    def lookup_names(self, cands: list[str], chapter_letters: str) -> list[tuple[str, str]]:
        for c in cands:
            hits = [(k, p) for k, p in self.by_name.get(c, []) if k[0] in chapter_letters]
            if hits:
                return sorted(set(hits))
        return []

    def lookup_kokuji(self, cands: list[str], chapter_letters: str, limit: int = 4) -> list[tuple[str, str]]:
        """Kubun **defined** in the notification text. Either of:

        - item line: the name appears at the head of a line starting with a number / kana / 注, such as
          `２ 高エネルギー放射線治療 …点` / `イ 臓器移植後` / `注５ …` (per line, whitespace removed)
        - body: appears as `機能強化加算として、80点を…` / `…加算の施設基準`
        A name matching more than 4 kubun is treated as a generic word and not taken.
        """
        for c in cands:
            if len(c) < 5:
                continue
            hits = []
            ce = re.escape(c)
            item = re.compile(r"^(?:\d{1,2}|[イロハニホヘトチリヌルヲ]|\(\d{1,2}\)|注\d{1,2})" + ce + r"(?:の場合|に係るもの|に限る)?.{0,12}$")
            phrase = re.compile(ce + r"(として|の施設基準|に係る施設基準)")
            for kub, path, (joined, lines) in self.kokuji:
                if kub[0] not in chapter_letters or c not in joined:
                    continue
                if phrase.search(joined) or any(item.match(ln) for ln in lines):
                    hits.append((kub, path))
            hits = sorted(set(hits))
            if 0 < len(hits) <= limit:
                return hits
        return []


def name_candidates(title: str) -> list[str]:
    """Heading → candidate names to match (tried in order).

    For a heading like `在宅時医学総合管理料の注 12（…）に規定する情報通信機器を用いた診療`, the part before
    「〜の注N」 is the kubun name. `Ａ及びＢ` `Ａ、Ｂ` are split only outside parentheses.
    """
    t = norm(title)
    base = re.sub(r"(の注\d+.*|に規定する.*|に関する施設基準.*|の施設基準.*|に係る.*|の届出に関する事項.*)$", "", t)
    out = [t, base]
    segs, cur, depth, i = [], "", 0, 0
    while i < len(base):
        for sep in ("及び", "並びに", "又は", "、", ","):
            if depth == 0 and base.startswith(sep, i):
                segs.append(cur)
                cur, i = "", i + len(sep)
                break
        else:
            ch = base[i]
            depth += ch in "（(" and 1 or 0
            depth -= ch in "）)" and 1 or 0
            cur += ch
            i += 1
    segs.append(cur)
    out += [s for s in segs if s]
    out += [strip_paren(s) for s in list(out)]
    # `精神科ショート・ケア「大規模なもの」` → `精神科ショート・ケア`
    out += [re.sub(r"「[^」]*」$", "", o) for o in list(out)]
    # `心臓カテーテル法による諸検査の血管内視鏡検査加算` → `心臓カテーテル法による諸検査`
    # (up to each 「の」, longest first. Only effective on an exact kubun name match, so rarely misfires)
    for o in list(out):
        for m in reversed(list(re.finditer("の", o))):
            out.append(o[: m.start()])
    return [o for o in dict.fromkeys(out) if len(o) >= 3]


def match_tensu(title: str, idx: TensuIndex, chapter_letters: str) -> tuple[list[tuple[str, str]], str]:
    """(list of kubun, kind of match)."""
    m = TSUSOKU_REF_RE.search(title)
    if m:
        letter = PART_LETTER_BY_NUM.get(_int(m.group(1)))
        if letter and letter in idx.part_dir:
            return [(f"{letter}通則", idx.part_dir[letter])], "通則"
    cands = name_candidates(title)
    hits = idx.lookup_names(cands, chapter_letters)
    if hits:
        return hits, "名称"
    hits = idx.lookup_kokuji(cands, chapter_letters)
    if hits:
        return hits, "告示本文"
    return [], ""


# ---------------------------------------------------------------------------
# Registrant list (intranet)
# ---------------------------------------------------------------------------

KUBUN_TOKEN_RE = re.compile(r"^(歯科?)?\s*([A-Z])\s*(\d{3}(?:-\d{1,2}){0,2})?\s*(通則)?\s*(.*)$")


def parse_kubun_cell(cell: str) -> list[dict]:
    """`A207 1` / `A000 注1,A002 注2` / `K 通則19,K475` / `歯科M029 注3` → tokens."""
    out = []
    for tok in re.split(r"[,，]", unicodedata.normalize("NFKC", cell)):
        tok = tok.strip()
        if not tok:
            continue
        m = KUBUN_TOKEN_RE.match(tok)
        if not m:
            out.append({"raw": tok, "dental": False, "letter": "", "kubun": "", "tsusoku": False, "rest": tok})
            continue
        dental, letter, num, ts, rest = m.groups()
        out.append({
            "raw": tok, "dental": bool(dental), "letter": letter,
            "kubun": f"{letter}{num}" if num and not dental else "",
            "tsusoku": bool(ts), "rest": (rest or "").strip(),
        })
    return out


def kubun_slug(cell: str) -> str:
    """Kubun cell → filename prefix. `A207 1` → `A207_1`, `A000 注1,A002 注2` → `A000注1・A002注2`."""
    s = unicodedata.normalize("NFKC", cell)
    parts = [re.sub(r"\s+", "_", re.sub(r"\s*(注|通則)\s*", r"\1", p.strip())) for p in re.split(r"[,，]", s)]
    return fs_safe("・".join(p for p in parts if p), 40)


def load_touroku() -> dict:
    info = json.loads((CACHE / "touroku.json").read_text(encoding="utf-8"))
    groups: dict[tuple[str, str], list[dict]] = {}
    order: list[tuple[str, str]] = []
    for r in info["rows"]:
        k = (r["kubun"], r["name"])
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(r)
    info["groups"] = [{"kubun": k, "name": nm, "rows": groups[(k, nm)], "tokens": parse_kubun_cell(k)} for k, nm in order]
    return info


def touroku_part(g: dict) -> str:
    """Registrant list row → location (part). Everything dental goes to `歯科`."""
    toks = g["tokens"]
    if all(t["dental"] or not t["letter"] for t in toks):
        return "歯科"
    t = next(t for t in toks if t["letter"] and not t["dental"])
    return t["letter"]


def touroku_k_subdir(g: dict, idx: "TensuIndex") -> str:
    """There are 141 K (surgery) registration rows, so place them in the same subsection directories as the
    facility standard side (kubun number → subsection from the fee schedule node path; general rules → `00_通則`)."""
    for t in g["tokens"]:
        if t["tsusoku"] and t["letter"] == "K":
            return "00_通則"
        path = idx.by_kubun.get(t["kubun"], "")
        m = re.search(r"/K_手術/第01節_手術料/第(\d+)款_", path)
        if m:
            return next(d for d in K_SUBDIRS if d.startswith(f"{int(m.group(1)):02d}_"))
        m = re.search(r"/K_手術/第0([23])節_", path)
        if m:
            return "14_輸血料" if m.group(1) == "2" else "15_手術医療機器等加算"
    sys.exit(f"cannot decide the subsection for a K row of the registrant list: {g['kubun']} {g['name']}")


# ---------------------------------------------------------------------------
# Matching facility standards ⇄ registrant list
# ---------------------------------------------------------------------------


def _name_keys(name: str) -> list[str]:
    """Facility standard name on the registrant list (may be juxtaposed as `ａ,ｂ`) → match keys. Also the form with trailing `１`/`(…)` dropped."""
    out = []
    for part in re.split(r"[,，]", name):
        p = norm(part)
        if not p:
            continue
        out.append(p)
        q = re.sub(r"[0-9１-９]+$", "", strip_paren(p))
        q = re.sub(r"(のイ|のロ|のハ|の\d+|イ|ロ|ハ)$", "", q)
        if q and q != p:
            out.append(q)
    return list(dict.fromkeys(out))


def link_touroku(node: dict, groups: list[dict], *, title: str | None = None, body: str | None = None) -> list[dict]:
    """Registrant list row groups corresponding to a facility standard node (heading name + matched kubun).

    1. name: a candidate name of the heading equals a key of the registered name, or registered name = candidate + digits (candidate 5+ chars)
    2. kubun: the heading matched **exactly one kubun**, and the row's kubun is that kubun itself (no 注 / item)
    3. body: the registered name appears in the circular body as `<名称>の施設基準` / `<名称>に関する施設基準`
       (add-ons defined in a 注 have their facility standard in an item inside a 「第N」: `A246 注7 入院時支援加算` is
       in the item of 第26の5 入退院支援加算). When the heading matched a kubun and the row also has a kubun number,
       only rows whose kubun agrees are taken (so an ICU add-on is not tied to emergency care)
    Passing `title` / `body` matches on those instead of the heading (for child nodes split per item).
    """
    cands = [c for c in name_candidates(title or node["title"]) if len(c) >= 4]
    kubuns = {k for k, _p in node["tensu"] if re.match(r"^[A-Z]\d{3}", k)}
    text = norm(body if body is not None else "\n".join(node["lines"]))
    hits = []
    for g in groups:
        keys = _name_keys(g["name"])
        # Prefix match is only "registered name = candidate + digits" (`診療録管理体制加算１`). Allowing a
        # parenthetical continuation would also tie `腹腔鏡下肝切除術(部分切除…)` to the facility standard of
        # `腹腔鏡下肝切除術(内視鏡手術用支援機器…)`
        by_name = any(
            k == c or (len(c) >= 5 and k.startswith(c) and re.match(r"^[0-9]", k[len(c):]))
            for k in keys for c in cands
        )
        bare = [t["kubun"] for t in g["tokens"] if t["kubun"] and not t["rest"]]
        by_kubun = len(kubuns) == 1 and node["kind"] != "通則" and bool(kubuns & set(bare))
        by_body = False
        if not by_name and not by_kubun:
            row_kubuns = {t["kubun"] for t in g["tokens"] if t["kubun"]}
            if not kubuns or not row_kubuns or (kubuns & row_kubuns):
                by_body = any(
                    len(k) >= 5 and re.search(re.escape(k) + r"(の施設基準|に関する施設基準|の基準)", text)
                    for k in keys
                )
        if by_name or by_kubun or by_body:
            hits.append(g)
    return hits


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def fm_lines(title: str, kijun: str, kubun: str, section: str, *, fields: bool) -> list[str]:
    out = [
        "---",
        f"title: {yaml_str(title)}",
        f"kijun: {yaml_str(kijun)}",
        f"kubun: {yaml_str(kubun)}",
        f"section: {yaml_str(section)}",
        f"source: {SOURCE}",
        f"retrieved_at: '{RETRIEVED}'",
    ]
    if fields:
        out += ["fields:", "  kijun: str", "  kubun: str", "  section: str", "  source?: str", "  retrieved_at?: date"]
    out.append("---")
    return out


def kubun_range(kubuns: list[str]) -> str:
    ks = sorted({k for k in kubuns if re.match(r"^[A-Z]\d{3}", k)})
    if not ks:
        return ""
    return ks[0] if len(ks) == 1 else f"{ks[0]}〜{ks[-1]}"


def caveat(name: str, touroku_paths: list[str], gigi_paths: list[str] | None = None) -> list[str]:
    """Disclaimer common to all leaves (judge facility standard compliance together with the notification status).

    `gigi_paths` holds the paths only when a Q&A node **exists** for the matched kubun (otherwise point to the section).
    """
    out = []
    if touroku_paths:
        out.append(
            "> **施設基準の充足は当院の届出状況と併せて判断すること。** 当院の登録状況は "
            + " / ".join(f"`{leaf_ref(p)}`" for p in touroku_paths)
            + " を read する (施設基準登録者一覧・令和8年7月時点。最新は"
            f" {TOUROKU_CONTACT} へ)。"
        )
    else:
        out.append(
            "> **施設基準の充足は当院の届出状況と併せて判断すること。** 当院の登録状況は "
            f"`{dir_ref(f'/院内マニュアル/{SEC_TOUROKU}')}` を施設基準名で grep_doc して確認する "
            f"(施設基準登録者一覧・令和8年7月時点。無ければ {TOUROKU_CONTACT} へ)。"
        )
    gigi = (
        " / ".join(f"`{tree_ref(g)}`" for g in gigi_paths) + " を read する"
        if gigi_paths
        # **Do not write a placeholder in path form.** `<部>/<区分>` in backticks could be passed
        # to read_doc as-is, so name the section and send them to grep
        else f"隣のセクション `{dir_ref(f'/診療報酬/{GIGI}')}` をこの施設基準の名称で grep_doc する"
        " (区分番号ごとにノードが分かれており、区分番号の読めない問は 区分番号なし の下)"
    )
    out.append(
        "> このノードは**施設基準通知の本文だけ**。点数と算定要件 (告示・留意事項通知) は"
        f"下の点数表ノードにある。疑義解釈 (Q&A) は {gigi} ({GIGI_AS_OF}。以降は厚労省の改定特設ページ)。"
    )
    return out


GIGI_INDEX: dict[str, str] = {}


def gigi_paths(node: dict) -> list[str]:
    """Paths of Q&A nodes that exist for the matched kubun."""
    return sorted({GIGI_INDEX[k] for k, _p in node["tensu"] if k in GIGI_INDEX})


def tensu_block(node: dict, conf: dict) -> list[str]:
    out = ["## 対応する点数表の区分", ""]
    if node["tensu"]:
        for kub, path in node["tensu"]:
            out.append(f"- `{tree_ref(path)}` ({kub})")
        out.append("")
        out.append(f"(見出しの名称と点数表ノードの{node['kind']}で突き合わせた)")
    else:
        out.append(
            f"名称から点数表の区分を自動で突き合わせられなかった。`{dir_ref('/診療報酬/' + conf['tensu'])}` を"
            " grep_doc で引くこと (通知の見出しが点数表の区分名と一致しない施設基準 —"
            " 注に規定する加算・歯科・調剤など)。"
        )
    return out


def touroku_block(paths: list[str]) -> list[str]:
    out = ["## 当院の届出状況 (登録者一覧) の参照先", ""]
    if paths:
        out += [f"- `{leaf_ref(p)}`" for p in paths]
    else:
        out.append(
            f"自動で突き合わせられた登録者一覧のノードは無い。`{dir_ref(f'/院内マニュアル/{SEC_TOUROKU}')}` を"
            f"施設基準名で grep_doc して確認し、無ければ {TOUROKU_CONTACT} へ。"
        )
    return out


def leaf_position(conf: dict, node: dict, where: str) -> str:
    return (
        f"「{conf['title']}」({conf['number']}、令和8年度改定{NOTE_SUFFIX[conf['pdf']]}) "
        + (f"{where} 「{key_label(node['key'])} {node['title']}」(原本 p.{node['page']})。" if not node.get("whole") else f"{where} (別添全体で 1 ノード)。")
        + f"適用は {APPLIES_FROM} から。"
    )


def source_line(conf: dict, page: int) -> str:
    return f"出典: {conf['title']} ({conf['number']}) {MANIFEST['sources'][conf['pdf']]['url']} p.{page}"


def write_leaf(path: Path, conf: dict, node: dict, where: str, section: str, touroku_paths: list[str]) -> None:
    body = clean(node["lines"])
    kij = f"{section} (全体)" if node.get("whole") else key_label(node["key"])
    lines = fm_lines(node["title"], kij, ",".join(k for k, _p in node["tensu"]), section, fields=False)
    lines += ["", leaf_position(conf, node, where), ""]
    lines += caveat(conf["short"], touroku_paths, gigi_paths(node))
    lines += ["", *tensu_block(node, conf), "", *touroku_block(touroku_paths), ""]
    lines += ["## 施設基準通知の本文", "", source_line(conf, node["page"]), "", "```", body, "```", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def write_split(dirpath: Path, conf: dict, node: dict, subs: list[dict], where: str, section: str, touroku_paths: list[str], sub_links: list[list[str]]) -> int:
    """Large 第N: directory + _index.md (preamble, list of children) + children (per item). Returns the number of children.

    `sub_links[i]` are the paths of the registrant list nodes for child i (matched by the item name).
    """
    dirpath.mkdir(parents=True)
    rel = "/" + str(dirpath.relative_to("data/doc"))
    kub = ",".join(k for k, _p in node["tensu"])
    children = []
    for i, s in enumerate(subs, 1):
        t = sub_title(s["title"])
        fname = f"{i:02d}_{fs_safe(t, 40)}"
        children.append((fname, s, t))
    lines = fm_lines(node["title"], key_label(node["key"]), kub, section, fields=True)
    lines += ["", leaf_position(conf, node, where), ""]
    lines += caveat(conf["short"], touroku_paths, gigi_paths(node))
    lines += [
        "",
        f"**この施設基準は {len(clean(node['lines'])):,} 字と大きいので、通知の項 (`１`, `２`, …) ごとに"
        f"子ノードに分けてある。このノードには前置きしか無い。** 子は次の {len(children)} 本"
        " (項の番号と冒頭):",
        "",
    ]
    for fname, s, _t in children:
        lines.append(f"- `{leaf_ref(f'{rel}/{fname}')}` — {s['label']} {s['title'][:60]}")
    lines += ["", *tensu_block(node, conf), "", *touroku_block(touroku_paths), ""]
    pre = clean(node.get("head_lines", []))
    if pre:
        lines += ["## 前置き (通知の本文)", "", source_line(conf, node["page"]), "", "```", pre, "```", ""]
    (dirpath / "_index.md").write_text("\n".join(lines), encoding="utf-8")
    for (fname, s, t), tp in zip(children, sub_links):
        cl = fm_lines(f"{s['label']} {t}", f"{key_label(node['key'])} の {s['label']}", kub, section, fields=False)
        cl += [
            "",
            f"「{conf['title']}」{where} 「{key_label(node['key'])} {node['title']}」の項 {s['label']}。"
            f"**親 `{dir_ref(rel)}` の一部** (前置きと他の項は親と兄弟にある)。",
            "",
        ]
        cl += caveat(conf["short"], tp, gigi_paths(node))
        cl += ["", *tensu_block(node, conf), "", *touroku_block(tp), "", "## 施設基準通知の本文", "", source_line(conf, node["page"]), "", "```", clean(s["lines"]), "```", ""]
        (dirpath / f"{fname}.md").write_text("\n".join(cl), encoding="utf-8")
    return len(children)


def dir_index(dirpath: Path, title: str, kijun: str, kubun: str, section: str, intro: list[str], entries: list[tuple[str, str, str]], preamble: str = "", conf: dict | None = None, page: int = 0, subdirs: list[tuple[str, str, int]] | None = None) -> None:
    lines = fm_lines(title, kijun, kubun, section, fields=True)
    lines += ["", *intro, ""]
    if subdirs:
        lines += ["## 下のディレクトリ", "", "| ディレクトリ | 中身 | ノード |", "|---|---|---:|"]
        for dname, dtitle, n in subdirs:
            lines.append(f"| `{dname}` | {dtitle} | {n} |")
        lines.append("")
    if entries:
        lines += ["## 収録している施設基準", "", "| 見出し | 施設基準 | 点数表の区分 |", "|---|---|---|"]
        for label, t, kub in entries:
            lines.append(f"| {label} | {t} | {kub or '-'} |")
    if preamble:
        assert conf is not None
        lines += ["", "## 前置き (通知の本文)", "", source_line(conf, page), "", "```", preamble, "```"]
    lines.append("")
    (dirpath / "_index.md").write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# Registrant list output
# ---------------------------------------------------------------------------

TOUROKU_NOTE = (
    "> **取扱注意**: 院内イントラ (his-info) 由来。院外への持ち出し・外部公開・再配布をしない。\n"
    "> この一覧は**施設基準ごとに専任・専従で登録している職員の名簿** (医事課施設基準係) であって、"
    "当院が届け出ている施設基準の全リストではない。職員配置を要しない施設基準 (地域加算・療養環境加算等) は"
    "届け出ていても載らない。**令和8年7月時点**の登録なので、最新の届出状況・異動は "
    f"{TOUROKU_CONTACT} に確認すること。"
)


def touroku_fm(title: str, kubun: str, staff: int, *, fields: bool) -> list[str]:
    out = ["---", f"title: {yaml_str(title)}", f"kubun: {yaml_str(kubun)}", f"staff: {staff}"]
    if fields:
        out += ["fields:", "  kubun: str", "  staff: int"]
    out.append("---")
    return out


def write_touroku(info: dict, links: dict[int, list[tuple[str, str]]], idx: TensuIndex) -> dict[int, str]:
    """Write the registrant list nodes. Returns: group index → path."""
    root = MANUAL / SEC_TOUROKU
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    groups = info["groups"]
    by_part: dict[str, list[int]] = defaultdict(list)
    for gi, g in enumerate(groups):
        by_part[touroku_part(g)].append(gi)
    paths: dict[int, str] = {}
    part_dirs = []
    for part in sorted(by_part, key=lambda p: (p == "歯科", p)):
        if part == "K":
            # One level deeper by subsection (same directory names as K_手術 on the facility standard side)
            sub_of = {gi: touroku_k_subdir(groups[gi], idx) for gi in by_part["K"]}
            for sub in sorted(set(sub_of.values())):
                dname = f"K_{PART_NAME['K']}/{sub}"
                (root / dname).mkdir(parents=True)
                part_dirs.append((dname, "K", [gi for gi in by_part["K"] if sub_of[gi] == sub]))
            continue
        dname = f"{part}_{PART_NAME[part]}" if part != "歯科" else "歯科"
        (root / dname).mkdir()
        part_dirs.append((dname, part, by_part[part]))
    for dname, part, gis in part_dirs:
        used: set[str] = set()
        for gi in gis:
            g = groups[gi]
            fname = f"{kubun_slug(g['kubun'])}_{fs_safe(g['name'], 40)}"
            base, n = fname, 2
            while fname in used:
                fname = f"{base}_{n}"
                n += 1
            used.add(fname)
            paths[gi] = f"/院内マニュアル/{SEC_TOUROKU}/{dname}/{fname}"
    for dname, part, gis in part_dirs:
        d = root / dname
        for gi in gis:
            g = groups[gi]
            rows = g["rows"]
            kub_tokens = [t["kubun"] for t in g["tokens"] if t["kubun"]]
            lines = touroku_fm(g["name"], g["kubun"], len(rows), fields=False)
            lines += [
                "",
                f"北海道大学病院「施設基準登録者一覧」(医科+歯科、{info['as_of']}時点、原本 p.{rows[0]['page']}) のうち、"
                f"区分 `{g['kubun']}` 「{g['name']}」として登録されている職員 ({len(rows)} 名)。",
                "",
                TOUROKU_NOTE,
                "",
                "## 対応する施設基準 (通知の本文)",
                "",
            ]
            if links.get(gi):
                lines += [f"- `{p}` ({t})" for p, t in links[gi]]  # p is already in output form (doc_ref applied by build_notice)
            else:
                lines.append(
                    f"自動で突き合わせられた施設基準ノードは無い。`{dir_ref(f'/診療報酬/{SEC_KIHON}')}` または"
                    f" `{dir_ref(f'/診療報酬/{SEC_TOKKEI}')}` を施設基準名で grep_doc すること。"
                )
            lines += ["", "## 対応する点数表の区分", ""]
            tp = []
            for t in g["tokens"]:
                if t["dental"]:
                    tp.append(f"- {t['raw']}: 歯科点数表 (未収録)")
                elif t["kubun"] and t["kubun"] in idx.by_kubun:
                    tp.append(f"- `{tree_ref(idx.by_kubun[t['kubun']])}` ({t['raw']})")
                elif t["tsusoku"] and t["letter"] in idx.part_dir:
                    tp.append(f"- `{tree_ref(idx.part_dir[t['letter']])}` ({t['raw']} — 部の通則)")
                elif t["kubun"]:
                    tp.append(f"- {t['raw']}: 点数表に該当ノードが見つからない")
                else:
                    tp.append(f"- {t['raw']}: 第1章の通則 (点数表ノードは無い)" if t["letter"] == "A" else f"- {t['raw']}")
            lines += tp
            lines += ["", f"## 登録職員 ({len(rows)} 名)", "", "| 氏名 | 職種 | 専任/専従 | 診療科 |", "|---|---|---|---|"]
            for r in rows:
                lines.append(f"| {r['person']} | {r['job']} | {r['role']} | {r['dept']} |")
            lines.append("")
            (d / (paths[gi].rsplit("/", 1)[1] + ".md")).write_text("\n".join(lines), encoding="utf-8")
        # Part _index
        kubs = [t["kubun"] for gi in gis for t in groups[gi]["tokens"] if t["kubun"]]
        staff = sum(len(groups[gi]["rows"]) for gi in gis)
        sub_label = K_SUBDIRS[dname.split("/")[1]] if "/" in dname else ""
        il = touroku_fm(
            (f"手術 {sub_label} の施設基準の登録状況" if sub_label else f"{PART_NAME[part]} の施設基準の登録状況") if part != "歯科" else "歯科点数表の施設基準の登録状況",
            kubun_range(kubs) if part != "歯科" else "歯科", staff, fields=True,
        )
        il += ["", f"施設基準登録者一覧 ({info['as_of']}時点) のうち、区分が `{part}` で始まる施設基準" + (f"の {sub_label}" if sub_label else "") + f" ({len(gis)} 種・登録 {staff} 名)。", "", TOUROKU_NOTE, "", "| 区分 | 施設基準 | 登録 |", "|---|---|---:|"]
        for gi in gis:
            g = groups[gi]
            il.append(f"| {g['kubun']} | {g['name']} | {len(g['rows'])} |")
        il.append("")
        (d / "_index.md").write_text("\n".join(il), encoding="utf-8")
    # Intermediate directory for K_手術
    k_dirs = [(dn, part, gis) for dn, part, gis in part_dirs if dn.startswith("K_")]
    if k_dirs:
        kl = touroku_fm("手術 の施設基準の登録状況", kubun_range([t["kubun"] for _d, _p, gis in k_dirs for gi in gis for t in groups[gi]["tokens"] if t["kubun"]]), sum(len(groups[gi]["rows"]) for _d, _p, gis in k_dirs for gi in gis), fields=True)
        kl += ["", f"施設基準登録者一覧 ({info['as_of']}時点) のうち、区分が `K` (手術) で始まる施設基準 ({sum(len(gis) for _d, _p, gis in k_dirs)} 種)。**点数表の款で 1 段切ってある** (施設基準側 `{dir_ref(f'/診療報酬/{SEC_TOKKEI}/K_手術')}` と同じ分け方)。", "", TOUROKU_NOTE, "", "| ディレクトリ | 中身 | 施設基準 |", "|---|---|---:|"]
        for dn, _p, gis in k_dirs:
            kl.append(f"| `{dn.split('/')[1]}` | {K_SUBDIRS[dn.split('/')[1]]} | {len(gis)} |")
        kl.append("")
        (root / "K_手術" / "_index.md").write_text("\n".join(kl), encoding="utf-8")

    # Section _index (parent = 院内マニュアル fields: source? / retrieved_at? / version?)
    total = sum(len(g["rows"]) for g in groups)
    sl = [
        "---",
        "title: '施設基準の届出状況 (施設基準登録者一覧 令和8年7月)'",
        f"source: '{his_doc_url(HIS_SOURCE)}'",
        f"retrieved_at: '{RETRIEVED}'",
        f"version: '{TOUROKU_VERSION}'",
        "fields:",
        "  kubun: str",
        "  staff: int",
        "---",
        "",
        f"北海道大学病院の**施設基準登録者一覧** (医科+歯科、{info['as_of']}時点、{info['pages']} ページ、"
        f"{TOUROKU_CONTACT})。院内イントラ his-info トップの「施設基準に関する職員の登録状況」の実体。"
        f"施設基準 {len(groups)} 種・登録職員 {total} 名分を、**1 ノード = 1 施設基準 (区分 × 施設基準名)** で収録した。",
        "",
        TOUROKU_NOTE,
        "",
        "## 使い方",
        "",
        f"- 「当院で〇〇加算を算定できるか」は、**施設基準の中身** (`{dir_ref(f'/診療報酬/{SEC_KIHON}')}` /"
        f" `{dir_ref(f'/診療報酬/{SEC_TOKKEI}')}`) と **`{dir_ref(f'/院内マニュアル/{SEC_TOUROKU}')}` の登録状況** の両方を read して答える。"
        "各ノードが相手の path を名乗っているので、どちらから入っても辿れる",
        "- 施設基準名で探すなら `grep_doc(query=\"<施設基準名>\", path=\"" + dir_ref(f"/院内マニュアル/{SEC_TOUROKU}") + "\")`。"
        "区分番号 (`A205-2` 等) は list の `kubun` に出る",
        "- **この一覧に無い = 届け出ていない、ではない** (職員登録を要しない施設基準は載らない)。"
        f"無ければ {TOUROKU_CONTACT} に確認する",
        "- 職員名は原本のまま。異動で変わるので、人を名指しするときは時点 (令和8年7月) を添える",
        "",
        "## ディレクトリ構成 (点数表の部)",
        "",
        "| ディレクトリ | 施設基準 | 登録 |",
        "|---|---:|---:|",
    ]
    for dname, part, gis in part_dirs:
        sl.append(f"| `{dname}` | {len(gis)} | {sum(len(groups[gi]['rows']) for gi in gis)} |")
    sl += ["", f"原本: [施設基準登録者一覧 (PDF)]({his_doc_url(MANIFEST['sources']['touroku.pdf']['url'])}) (his-info、postback 経由。sha256 {MANIFEST['sources']['touroku.pdf']['sha256'][:16]}…)", ""]
    (root / "_index.md").write_text("\n".join(sl), encoding="utf-8")
    return paths


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

MANIFEST: dict = {}
RETRIEVED = ""
NOTE_SUFFIX: dict[str, str] = {}


# 特掲診療料 K_手術 (211 headings) is further **cut by fee schedule subsection**. The subsection comes from the path
# of the fee schedule node matched by name; unmatched headings **inherit the previous heading's subsection** (the
# circular is in subsection order). Only general rules (`医科点数表第２章第 10 部手術の通則の５`) and dental surgeries are decided by number
K_SUBDIRS = {
    "00_通則": "手術の通則に規定する施設基準",
    "01_皮膚・皮下組織": "第1款 皮膚・皮下組織",
    "02_筋骨格系・四肢・体幹": "第2款 筋骨格系・四肢・体幹",
    "03_神経系・頭蓋": "第3款 神経系・頭蓋",
    "04_眼": "第4款 眼",
    "05_耳鼻咽喉": "第5款 耳鼻咽喉",
    "06_顔面・口腔・頸部": "第6款 顔面・口腔・頸部",
    "07_胸部": "第7款 胸部",
    "08_心・脈管": "第8款 心・脈管",
    "09_腹部": "第9款 腹部",
    "10_尿路系・副腎": "第10款 尿路系・副腎",
    "11_性器": "第11款 性器",
    "12_削除": "第12款 削除",
    "13_手術等管理料": "第13款 手術等管理料",
    "14_輸血料": "第2節 輸血料",
    "15_手術医療機器等加算": "第3節 手術医療機器等加算",
    "16_歯科点数表の手術": "歯科点数表 第9部 手術 (医科点数表に無い)",
}
K_DENTAL_KEYS = {(80, 6), (80, 7), (80, 8), (80, 9), (80, 10)}  # 歯周組織再生誘導手術 … 口腔粘膜血管腫凝固術 (dental)


def k_subdir(b: dict, prev: str) -> str:
    if b["key"] in K_DENTAL_KEYS:
        return "16_歯科点数表の手術"
    if b["kind"] == "通則":
        return "00_通則"
    for _k, path in b["tensu"]:
        m = re.search(r"/K_手術/第01節_手術料/第(\d+)款_", path)
        if m:
            return next(d for d in K_SUBDIRS if d.startswith(f"{int(m.group(1)):02d}_"))
        m = re.search(r"/K_手術/第0([23])節_", path)
        if m:
            return "14_輸血料" if m.group(1) == "2" else "15_手術医療機器等加算"
        if path.endswith("/K_手術"):
            return "00_通則"
    return prev


def build_notice(name: str, idx: TensuIndex, touroku: dict, touroku_paths: dict[int, str], links_back: dict[int, list[tuple[str, str]]]) -> dict:
    conf = NOTICES[name]
    letters = "A" if name == "kihon" else "BCDEFGHIJKLMNO"
    parsed = split_notice(name)
    root = CATEGORY / conf["section"]
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    stats = defaultdict(int)
    groups = touroku["groups"]

    # Decide where each node goes: (dir relative path, dir title, section label, block)
    placed: list[tuple[str, str, str, dict]] = []
    for b in parsed["honbun"]:
        placed.append((HONBUN_DIR, "通知本文 (施設基準等の総則・届出手続き・受理後の措置・経過措置)", "通知本文", b))
    for num, bt in parsed["betten"].items():
        title, dname = conf["betten"][num]
        if dname is not None:
            if not bt["blocks"]:
                # An attachment with no 第N headings (like 別添5) = 1 leaf
                placed.append((dname, title, f"別添{num} {title}", {"key": (0,), "title": title, "lines": bt["preamble"], "line_no": 0, "page": 0, "deleted": False, "whole": True}))
                bt["preamble"] = []
            for b in bt["blocks"]:
                placed.append((dname, title, f"別添{num} {title}", b))
        else:
            for b in bt["blocks"]:
                part = PART_OVERRIDE.get(b["key"])
                if part is None:
                    for bkey, pdir, plabel in TOKKEI_PARTS:
                        if b["key"] >= bkey:
                            part = (pdir, plabel)
                if part is None:
                    sys.exit(f"{name}: {key_label(b['key'])} falls into no part of TOKKEI_PARTS")
                placed.append((part[0], part[1], f"別添{num} {title} — {part[1]}", b))

    # Matching (fee schedule)
    for dname, _dt, _sec, b in placed:
        if b["deleted"]:
            b["tensu"], b["kind"] = [], ""
        else:
            b["tensu"], b["kind"] = match_tensu(b["title"], idx, letters)

    # K_手術 goes one level deeper by subsection (match result → inherited)
    prev = "01_皮膚・皮下組織"
    for i, (dname, dtitle, sec, b) in enumerate(placed):
        if dname == "K_手術":
            sub = prev if b["deleted"] else k_subdir(b, prev)
            prev = sub
            placed[i] = (f"K_手術/{sub}", K_SUBDIRS[sub], f"{sec} {K_SUBDIRS[sub]}", b)

    # Matching (registrant list)
    for dname, _dt, _sec, b in placed:
        b["touroku"] = [] if (b["deleted"] or dname == HONBUN_DIR) else link_touroku(b, groups)

    # Writing out
    by_dir: dict[str, list[tuple[str, str, dict]]] = defaultdict(list)
    for dname, dtitle, sec, b in placed:
        by_dir[dname].append((dtitle, sec, b))
    dir_rows: dict[str, tuple[str, int]] = {}
    for dname, items in by_dir.items():
        d = root / dname
        d.mkdir(parents=True, exist_ok=True)
        entries = []
        used: set[str] = set()
        for dtitle, sec, b in items:
            if b["deleted"]:
                stats["deleted"] += 1
                continue
            tpaths = sorted({touroku_paths[groups.index(g)] for g in b["touroku"]})
            if b.get("whole"):
                fname = fs_safe(b["title"], 50)
            else:
                fname = f"{key_fname(b['key'])}_{fs_safe(b['title'])}"
            if fname in used:
                sys.exit(f"{name}: filename collision: {dname}/{fname}")
            used.add(fname)
            where = sec.split(" — ")[0] if not b.get("whole") else sec
            body_len = len(clean(b["lines"]))
            subs = split_subs(b) if body_len > BLOCK_SPLIT else None
            node_rel = f"/診療報酬/{conf['section']}/{dname}/{fname}"
            label = key_label(b["key"]) if not b.get("whole") else "(全体)"
            if subs:
                # Link the registrant list to each per-item child by the item name too (add-ons defined in a 注 live here).
                # The parent names the children's links as well, and the registrant list side names both parent and child
                sub_links = []
                for s_ in subs:
                    gs = [] if dname == HONBUN_DIR else link_touroku(b, groups, title=s_["title"], body="\n".join(s_["lines"]))
                    sub_links.append(sorted({touroku_paths[groups.index(g)] for g in gs}))
                    for g in gs:
                        if g not in b["touroku"]:
                            b["touroku"].append(g)
                tpaths = sorted({touroku_paths[groups.index(g)] for g in b["touroku"]})
                n = write_split(d / fname, conf, b, subs, where, sec, tpaths, sub_links)
                for (i_, s_), tp in zip(enumerate(subs, 1), sub_links):
                    for g in groups:
                        if touroku_paths[groups.index(g)] in tp:
                            links_back[groups.index(g)].append((leaf_ref(f"{node_rel}/{i_:02d}_{fs_safe(sub_title(s_['title']), 40)}"), f"{label} {b['title']} の {s_['label']}"))
                stats["split"] += 1
                stats["split_children"] += n
            else:
                write_leaf(d / f"{fname}.md", conf, b, where, sec, tpaths)
            stats["nodes"] += 1
            stats["matched"] += bool(b["tensu"])
            stats["kind_" + (b["kind"] or "なし")] += 1
            stats["linked"] += bool(b["touroku"])
            stats["max_body"] = max(stats["max_body"], body_len)
            entries.append((label, b["title"], ",".join(k for k, _p in b["tensu"])))
            for g in b["touroku"]:
                links_back[groups.index(g)].append((doc_ref(node_rel, is_dir=bool(subs)), f"{label} {b['title']}"))
        # Directory _index
        dtitle, sec, _b = items[0]
        kubs = [k for _l, _t, ks in entries for k in ks.split(",") if k]
        pre = ""
        page = 0
        m = re.match(r"別添(\d+)", sec)
        if m and conf["betten"][m.group(1)][1] is not None:
            pre = clean(parsed["betten"][m.group(1)]["preamble"])
            page = _b["page"]
        intro = [
            f"「{conf['title']}」({conf['number']}) の {sec.split(' — ')[0]}"
            + (f" のうち {sec.split(' — ')[1]} に当たる施設基準" if " — " in sec else "")
            + f"。**1 ノード = 通知の「第N」1 つ = 1 施設基準** ({len(entries)} 本)。",
            "",
            f"施設基準の充足は当院の届出状況 (`{dir_ref(f'/院内マニュアル/{SEC_TOUROKU}')}`) と併せて判断すること。"
            "各リーフが対応する点数表の区分と登録状況ノードの path を名乗る。",
        ]
        # **Do not give the parent directory and a direct leaf the same title.** For a directory with
        # a single entry (別添5 短期滞在手術等基本料) dtitle would equal the only leaf's title, and
        # parent and child would look identical in the /doc/list listing
        if any(t == dtitle for _l, t, _k in entries):
            dtitle = f"{dtitle} (親ディレクトリの案内)"
        dir_index(d, dtitle, sec.split(" — ")[0] if " — " not in sec else sec.split(" — ")[1], kubun_range(kubs), sec, intro, entries, pre, conf, page)
        stats["dirs"] += 1
        stats["max_children"] = max(stats["max_children"], len(entries))
        dir_rows[dname] = (dtitle, len(entries))

    # _index of the intermediate directory (K_手術): only the list of subdirectories
    section_rows: list[tuple[str, str, int]] = []
    for dname in list(by_dir):
        top = dname.split("/")[0]
        if "/" in dname and top not in dir_rows and top not in {r[0] for r in section_rows}:
            subs_ = [(sd.split("/")[1], dir_rows[sd][0], dir_rows[sd][1]) for sd in by_dir if sd.startswith(top + "/")]
            n_all = sum(n for _a, _b2, n in subs_)
            plabel = next(pl for _k, pd, pl in TOKKEI_PARTS if pd == top)
            kubs = [k for sd in by_dir if sd.startswith(top + "/") for _dt, _s, b in by_dir[sd] for k, _p in b["tensu"] if not b["deleted"]]
            dir_index(
                root / top, plabel, f"別添1 {plabel}", kubun_range(kubs), f"別添1 {conf['betten']['1'][0]} — {plabel}",
                [f"「{conf['title']}」({conf['number']}) の別添1 のうち {plabel} に当たる施設基準 ({n_all} 本)。"
                 "**点数表の款 (皮膚 → 筋骨格 → … → 性器) と、通則・輸血・手術医療機器等加算・歯科でディレクトリを切ってある** — 見出しの名称で点数表の区分に当たったものはその款、当たらなかったものは通知の並び (款順) で直前の見出しの款に置いている。",
                 "", f"施設基準の充足は当院の届出状況 (`{dir_ref(f'/院内マニュアル/{SEC_TOUROKU}')}`) と併せて判断すること。"],
                [], subdirs=subs_,
            )
            stats["dirs"] += 1
            section_rows.append((top, plabel, n_all))
        elif "/" not in dname:
            section_rows.append((dname, dir_rows[dname][0], dir_rows[dname][1]))

    # Section _index (parent `/診療報酬` fields: tensuhyo / publisher / applies_from / source? / retrieved_at?)
    src = MANIFEST["sources"][conf["pdf"]]
    sl = [
        "---",
        f"title: {yaml_str(conf['short'] + ' (令和8年度改定 ' + conf['number'] + ')')}",
        f"tensuhyo: {yaml_str(conf['title'] + ' (' + conf['number'] + ')')}",
        f"publisher: {PUBLISHER}",
        f"applies_from: '{APPLIES_FROM}'",
        f"source: {SOURCE}",
        f"retrieved_at: '{RETRIEVED}'",
        "fields:",
        "  kijun: str",
        "  kubun: str",
        "  section: str",
        "  source?: str",
        "  retrieved_at?: date",
        "---",
        "",
        f"「{conf['title']}」({conf['number']}、令和8年度改定{NOTE_SUFFIX[conf['pdf']]}) の本文と別添。"
        f"{conf['chapter']}の「別に厚生労働大臣が定める施設基準」の**中身**がここにある。適用は {APPLIES_FROM} から。",
        "",
        "**1 ノード = 通知の「第N」1 つ = 1 施設基準** (`第１`, `第１の２`, `第 13 の２の２` の見出しで切っている)。"
        f"大きいもの ({BLOCK_SPLIT:,} 字超) は項ごとに子ノードに分け、親がその path を名乗る。",
        "",
        "## 使い方 (「当院で算定できるか」に答えるには両方 read する)",
        "",
        "1. **施設基準の中身** = このセクションのノード (通知の本文)",
        f"2. **当院の届出状況** = `{dir_ref(f'/院内マニュアル/{SEC_TOUROKU}')}` (施設基準登録者一覧・令和8年7月時点)。"
        "各ノードが相手の path を名乗っているので、どちらから入っても辿れる",
        f"3. **点数と算定要件** = `{dir_ref('/診療報酬/' + conf['tensu'])}` (告示・留意事項通知)。各ノードの"
        "「対応する点数表の区分」に path がある",
        "",
        "施設基準名で探すなら `grep_doc(query=\"<名称>\", path=\"" + dir_ref(f"/診療報酬/{conf['section']}") + "\")`。"
        "list の `kubun` に対応する区分番号、`kijun` に通知の見出し番号が出る。",
        "",
        "## ディレクトリ構成",
        "",
        "| ディレクトリ | 中身 | ノード |",
        "|---|---|---:|",
    ]
    for dname, dtitle, n in sorted(section_rows):  # same name order as the walker's list
        sl.append(f"| `{dname}` | {dtitle} | {n} |")
    if name == "tokkei":
        sl += ["", "`K_手術` は 211 本あるので、点数表の款でもう 1 段切ってある (`K_手術/01_皮膚・皮下組織` …)。歯科固有 (`歯科M_…` / `歯科N_…`) と `調剤` は医科点数表に部が無い。"]
    sl += [
        "",
        "## 収録していないもの",
        "",
        "- **様式 (届出書・別紙)**。通知の別添のうち届出書の様式は収録していない (原本 PDF を見る)",
        "- **施設基準の告示** (基本診療料の施設基準等 / 特掲診療料の施設基準等)。縦書き PDF でテキストが取れない。"
        "通知の別添が同じ基準を横書きで持っているのでそちらを収録した",
        f"- **疑義解釈 (Q&A)** — 隣のセクション `{dir_ref(f'/診療報酬/{GIGI}')}` にある ({GIGI_AS_OF}。以降は特設ページ)。各リーフが区分ごとの path を名乗る",
        "",
        "## 原本の版",
        "",
        f"- {src['note']} {src['url']} (sha256 {src['sha256'][:16]}…、{RETRIEVED} 取得)",
        "",
        "**通知の PDF は訂正版に差し替えられ続ける** (特設ページに「0730訂正後」と併記される)。上の版表記より新しい訂正が出ていることがある。",
        "",
    ]
    (root / "_index.md").write_text("\n".join(sl), encoding="utf-8")
    stats["headings"] = sum(1 for _d, _t, _s, b in placed if not b.get("whole"))
    print(
        f"{conf['section']}: headings={stats['headings']} deleted={stats['deleted']} nodes={stats['nodes']} "
        f"split={stats['split']}(+{stats['split_children']}) dirs={stats['dirs']} max_children={stats['max_children']} "
        f"matched={stats['matched']} (name {stats['kind_名称']} / notification text {stats['kind_告示本文']} / general rules {stats['kind_通則']}) "
        f"linked={stats['linked']} max_body={stats['max_body']}"
    )
    return stats


def main() -> None:
    global MANIFEST, RETRIEVED, NOTE_SUFFIX
    MANIFEST = json.loads((CACHE / "manifest.json").read_text(encoding="utf-8"))
    RETRIEVED = MANIFEST["retrieved_at"]
    for pdf in ("kihon.pdf", "tokkei.pdf"):
        m = re.search(r"（\d{4}訂正後）", MANIFEST["sources"][pdf]["note"])
        NOTE_SUFFIX[pdf] = "・" + m.group(0).strip("（）") if m else ""
    idx = TensuIndex()
    GIGI_INDEX.update(idx.gigi_by_kubun)
    if not GIGI_INDEX:
        print(f"  ! {CATEGORY / GIGI} missing or empty — Q&A paths will only point to the section", file=sys.stderr)
    touroku = load_touroku()
    # Fix the registrant list paths first (the facility standard side names them)
    links_back: dict[int, list[tuple[str, str]]] = defaultdict(list)
    touroku_paths = write_touroku(touroku, {}, idx)
    for name in ("kihon", "tokkei"):
        build_notice(name, idx, touroku, touroku_paths, links_back)
    # Write again to fill in the reverse direction (registrant list → facility standards)
    paths2 = write_touroku(touroku, links_back, idx)
    assert paths2 == touroku_paths
    n_linked = sum(1 for gi in range(len(touroku["groups"])) if links_back.get(gi))
    print(
        f"{SEC_TOUROKU}: groups={len(touroku['groups'])} rows={len(touroku['rows'])} "
        f"linked={n_linked} unlinked={len(touroku['groups']) - n_linked}"
    )


if __name__ == "__main__":
    main()
