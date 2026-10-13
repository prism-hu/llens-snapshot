#!/usr/bin/env python3
"""Convert the medical fee schedule (医科点数表) and friends into the doc tree
(cache/shinryo_hoshu → data/doc/診療報酬).

**1 node = 1 kubun (section) number**. Three inputs with different roles:

  - `kokuji.txt`  … the notification (kokuji) body 「診療報酬の算定方法」 Appendix 1. **Points and notes** (the backbone of billing)
  - `tsuchi.txt`  … the implementation notice (tsuchi) Attachment 1 「医科診療報酬点数表に関する事項」. **Billing requirements in prose**
  - `masters/s.csv` … medical procedure master. **Procedure codes and points as they appear on claims**

Kokuji and tsuchi are prose, so they are cut at line-head kubun-number anchors (`Ａ０００`);
the master is grouped by code-table number (chapter, part, kubun, branch). The three are
matched by kubun number.

## Six "chapters" are ingested (values of the master's 「コード表用番号 章」 column)

    ch1 basic fees           2,202 rows → 医科点数表R08_基本診療料          (kokuji + tsuchi + CSV)
    ch2 specific fees        9,220 rows → 医科点数表R08_特掲診療料          (kokuji + tsuchi + CSV)
    ch3 geriatric facility       2 rows → 医科点数表R08_介護老人保健施設入所者 (kokuji + tsuchi + CSV)
    ch6 forensic psychiatry    353 rows → 医療観察点数表R08                 (**CSV only**)
    ch8 inpatient meals         46 rows → 入院時食事療養R08                 (**CSV only**)
    ch9 not in fee schedule      8 rows → 点数表外コードR08                 (**CSV only**)

**Chapters 6/8/9 are not in the medical fee schedule (kokuji Appendix 1).** They belong to
other notifications (forensic psychiatry fee schedule / inpatient meal standards / internal
claim-system codes) that merely ride along in the master; grepping `kokuji.pdf` / `tsuchi.pdf`
yields nothing (0 hits for 医療観察, 0 for 入院時食事療養). **Do not name them "chapter 6 of the
fee schedule" from the chapter number alone** — the facts were confirmed against the description
of item 85 in `layout_rec1.pdf` (alphabet part `－` = 「第3章介護老人保健施設入所者に
係る診療料」「医療観察診療報酬点数表に規定する診療行為」「入院時食事療養及び入院時生活
療養」「標準負担額」, `＊` = 「医科点数表に区分番号の設定がない診療行為」).

## Chapter 2 hierarchy — keep the children of one list call at or below CHILD_LIMIT

Chapter 2 has 2,280 nodes; listing them flat under the section makes list unusable.
**Only the divisions present in the kokuji (part → section → sub-section → group) are used,
and only where the limit is exceeded do we go one level deeper** (`collapse()`). No home-made
classification. "Group" (群) = a parenthesised heading in the kokuji such as `（胃、十二指腸）`;
it carries no number, so **the sequence number in the file name is assigned here**
(`群01_…`, not a kokuji number).

Chapter 1 stays **flat** with its 136 nodes (so existing paths do not change).
125 vs 2,280 makes the structure asymmetric, which is fine (see `docs/cage/17_shinryo_hoshu.md`).

## Kubun with a large code table get "one more node"

Stacking kokuji + tsuchi + code table in one file makes A400 40,178 chars, far above the
largest existing corpus node (9K). **Only kubun whose code table exceeds `CODE_TABLE_SPLIT`
rows** get the code table as a **child node**:

    A400_短期滞在手術等基本料/
      _index.md      ← kokuji + tsuchi (this is the node body)
      コード表.md     ← procedure codes and points, 178 rows

**This is not one node split across two files** (the "node = one file" rule of `01_brief.md`
holds). Directory + `_index.md` is one node, `コード表.md` is another = **the node count grows**.

## Kubun granularity — **branch numbers count. Chapter 2 has two levels of branches**

`Ａ２３０` and `Ａ２３０－２` are **different kubun**. The master splits them into item 92
「区分番号」 (3 digits) and item 93 「枝番」 (2 digits); **ignoring the branch collapses 125 kubun into 78**.

Chapter 2 additionally has **23 three-level kubun numbers** such as `Ｂ００１－２－２`.
The master holds only one branch level, so the codes for `Ｂ００１－２` through `Ｂ００１－２－12`
**all sit under one branch (B001-2)**. The master offers no way to tell them apart, so
**the code table goes wholesale on the second-level node (B001-2), and the third-level nodes
state "codes are lumped under the parent branch" and name that path** (`CODE_TABLE_LUMPED`).
We never split by item number ourselves (that would invent a mapping absent from the source).

## Master columns (0-based. Item numbers follow layout_rec3.pdf 「医科診療行為マスター」)

| col | item | name |
|---|---|---|
| 2 | 3 | 診療行為コード (procedure code) |
| 4 | 5 | 省略漢字名称 (short name) |
| 10 | 11 | 点数識別 (point kind: **1:yen 3:points 4:purchase price 5:% add-on 6:% reduction 7:deduction 8:points (negative)**) |
| 11 | 12 | 新又は現点数 (current points) |
| 84 | 85 | コード表用番号 (alphabet part). `－` = ch3/forensic/meals, `＊` = no kubun number |
| 86 | 87 | 変更年月日 (changed on) |
| 87 | 88 | 廃止年月日 (abolished on; 99999999 = current) |
| 89 | 90 | コード表用番号 章 (chapter) |
| 90 | 91 | コード表用番号 部 (part) |
| 91 | 92 | コード表用番号 区分番号 (kubun number) |
| 92 | 93 | コード表用番号 枝番 (branch) |
| 93 | 94 | コード表用番号 項番 (item seq) |

**Never print the bare number while ignoring the point kind.** Chapter 1 alone has 163 rows of
kind 8 (negative points = reduction) and 43 of kind 6 (% reduction). Writing
「（選定療養）病床数が200以上の病院初診（200点減算）」 as `200点` flips the sign.

usage: uv run python scripts/build_shinryo_hoshu.py   (run from cage/)
"""

from __future__ import annotations

import csv
import json
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from textfix import doc_ref, safe_segment, static_ref  # noqa: E402

CACHE = Path("cache/shinryo_hoshu")
CATEGORY = Path("data/doc/診療報酬")

# Section (= directory right under the category) names. Bump R08 → R10 at the next revision
SEC_CH1 = "医科点数表R08_基本診療料"
SEC_CH2 = "医科点数表R08_特掲診療料"
SEC_CH3 = "医科点数表R08_介護老人保健施設入所者"
SEC_CH6 = "医療観察点数表R08"
SEC_CH8 = "入院時食事療養R08"
SEC_CH9 = "点数表外コードR08"

# Rough cap on children returned by one /doc/list. **Only where exceeded**, go one level deeper at a kokuji division
CHILD_LIMIT = 80

# Threshold for moving the code table to a child node (split when the row count **exceeds** this).
# Measured chapter-1 distribution: median 2 rows / max 304, with a clear valley between
# 56 (A001) and 46 (A108). 50 sits in that valley, so nudging it does not change the result.
CODE_TABLE_SPLIT = 50
# Child node file name (= node id). Path is `<section>/…/<kubun slug>/コード表`
CODE_CHILD = "コード表"

# The original procedure master CSV is also placed under data/static/. For exhaustive code
# filtering, pull the CSV into the sandbox and query with pandas instead of the doc tables.
# **The file name carries the version date** (the master is updated ad hoc)
MASTER_CSV = "s_20260901.csv"
MASTER_CSV_URL = static_ref(f"shinryo_hoshu/{MASTER_CSV}")
MASTER_CSV_DESC = (
    "医科診療行為マスター 令和8年9月1日版・cp932・ヘッダ行なし・150 列・全章分"
)

TENSUHYO = "医科診療報酬点数表 (令和8年度診療報酬改定)"
PUBLISHER = "厚生労働省"
APPLIES_FROM = "2026-06-01"  # Start of application for the body. Only drug prices start earlier, 2026-04-01
SOURCE = "https://www.mhlw.go.jp/stf/newpage_67729.html"
MASTER_SOURCE = "https://shinryohoshu.mhlw.go.jp/shinryohoshu/downloadMenu/"
RETRIEVED = "2026-09-04"

# Cutoff of the ingested Q&A (keep in sync with what `{{DOC_BASE}}/診療報酬/疑義解釈R08/` states from its manifest).
# **Q&A are never merged into the body, so the text tells readers to check the source beyond this**. Bump when その13 is ingested
QA_AS_OF = "その12 (2026-09-02 発出) まで"
QA_LATEST_HINT = "厚生労働省の改定特設ページ「３．疑義解釈・訂正通知」"

# Neighbouring sections (ingested 2026-09-05; `19_gigi_kaishaku.md` / `20_shisetsu_kijun.md`).
# Fee-schedule leaves **name the real path of the matching node by kubun number** — read returns
# only one node, so merely mentioning the section reads as "facility standards are not included".
# The build scans the neighbouring sections to resolve paths (absent: section pointer only + warning).
SEC_QA = "疑義解釈R08"
SEC_KIJUN_CH1 = "施設基準R08_基本診療料"
SEC_KIJUN_CH2 = "施設基準R08_特掲診療料"
TOUROKU_DOC = "/院内マニュアル/施設基準届出状況"
TOUROKU_DIR = Path("data/doc") / TOUROKU_DOC.lstrip("/")
TOUROKU_AS_OF = "令和8年7月時点"  # As-of date of the registrant list. Keep in sync with `build_shisetsu_kijun.py` when refetched
TOUROKU_CONTACT = "医事課施設基準係"
# DPC electronic fee table (ingested 2026-09-05; `21_dpc.md`). Only the inpatient-fee leaves
# (chapter 1 part 2) point there with one line "for bundled payment, go there" — baking it into
# every leaf just bloats 2,564 files, and DPC only matters for inpatient care
SEC_DPC = "DPC点数表R08"
DPC_SECTION_PREFIX = "第2部 入院料等"  # Prefix of the chapter-1 front-matter `section`


# Paths of other nodes written into bodies use the canonical form (`{{DOC_BASE}}/…/<name>.md`; directories `{{DOC_BASE}}/…/`).
# Internally they are held without the prefix as `/<category>/…` and pass through here **only on output**.
# Whether leaf or directory is known by the caller (shape of the node it writes / real files in the existing tree)
def dir_ref(path: str) -> str:
    return doc_ref(path, is_dir=True)


def leaf_ref(path: str) -> str:
    return doc_ref(path, is_dir=False)
# Cap on listed facility-standard nodes when one kubun has many (B001 has 29). The rest get a grep hint
XREF_LIST_MAX = 8

# ---------------------------------------------------------------------------
# Full-width → half-width. Kubun numbers are `Ａ０００` in the source, `A000` in paths / front matter
# ---------------------------------------------------------------------------
_Z2H = str.maketrans(
    "".join(chr(0xFF10 + i) for i in range(10))
    + "".join(chr(0xFF21 + i) for i in range(26)),
    "0123456789" + "".join(chr(65 + i) for i in range(26)),
)

# Line-head kubun-number anchor.
#   - The dash varies in the source (U+FF0D / U+2212 / U+2010 …), so list the candidates
#   - **Up to two branch levels**. Chapter 2 has the `Ｂ００１－２－２` form (23 of them)
#   - **Two-digit branches appear as half-width digits** (`Ｂ００１－10`). Full-width only loses 34 kubun.
#     Keep the 3-digit part full-width — allowing half-width misreads the appendix table at the
#     end of the tsuchi (`Ａ221-3 産科管理`) as body anchors and yields 78 duplicates
_DASH = "－−‐―-"
ANCHOR_RE = re.compile(
    rf"^[\s\x0c]*([Ａ-Ｚ][０-９]{{3}}(?:[{_DASH}][０-９0-9]{{1,2}}){{0,2}})[\s　]+(\S.*)$"
)
# A bare kubun number (catches a second one mid-line: `Ｋ０００ 創傷処理、Ｋ０００－２ 小児創傷処理`)
KUBUN_TOKEN_RE = re.compile(rf"[Ａ-Ｚ][０-９]{{3}}(?:[{_DASH}][０-９0-9]{{1,2}}){{0,2}}")
# Chapter / part / section / sub-section headings. **Trailing whitespace is required** — otherwise a
# wrapped body line like 「第３章介護老人保健施設入所者に係る…」 is misread as a heading.
# **Two-digit numbers are half-width**, and the tsuchi pads them as `第 10 部   手術`
HEADING_RE = re.compile(r"^[\s\x0c]*第[\s　]*([０-９0-9]{1,3})[\s　]*([章部節款])[\s　]+(\S.*)$")
# Kokuji "group" (群). Unnumbered headings like `（胃、十二指腸）` sit under a sub-section.
# Surgery / procedures / in-vivo tests cannot get under CHILD_LIMIT by sub-section alone, so use these.
# **Parenthesised lines also occur in the body** (`（生活療養を受ける場合にあっては、1,116点）`), so
# reject lines containing digits and require the next real line to be an anchor or `通則`
GROUP_RE = re.compile(r"^[\s\x0c]{4,20}（([^（）]{2,30})）\s*$")
# Preamble that belongs to no kubun
TSUUSOKU_RE = re.compile(r"^[\s\x0c]*[＜<]?通則[＞>]?\s*$")
KUBUN_RE = re.compile(r"^[\s\x0c]*区分\s*$")
# Page footer of the tsuchi (`         - 42 -`)
FOOTER_RE = re.compile(r"^\s*-\s*\d+\s*-\s*$")
# With `-layout` extraction, ruby comes out as a separate line above the main line (`ぼうこう` / `ふん`).
# **Several ruby tokens share one line** (`   くう      くう`), so assuming a single token
# misses 30%. The dropped count is printed on the build's last line, so keep an eye on it
RUBY_RE = re.compile(r"^\s{4,}[ぁ-んー]{1,4}(?:\s+[ぁ-んー]{1,4})*\s*$")
# Whether the heading line ends with points/amount (`… 2,004点`). If so, that kubun is
# complete in one line and does not share a body with the next
SELF_POINT_RE = re.compile(r"[0-9０-９,，]+[点円]\s*$")

_LOWER = {"章": "部節款群", "部": "節款群", "節": "款群", "款": "群", "群": ""}
_LEVELS = ("部", "節", "款", "群")


def kubun_key(raw: str) -> str:
    """`Ａ２３０－２` → `A230-2`, `Ｂ００１－２－10` → `B001-2-10`. Leading zeros are dropped."""
    s = re.sub(f"[{_DASH}]", "-", raw.translate(_Z2H))
    m = re.match(r"^([A-Z])(\d{3})((?:-\d{1,2})*)$", s)
    if not m:
        raise ValueError(f"not a kubun number: {raw!r}")
    tail = "".join(f"-{int(x)}" for x in m.group(3).split("-") if x)
    return m.group(1) + m.group(2) + tail


def strip_points(title: str) -> str:
    """Strip the trailing points and the "formula column" from a kokuji heading line.

    Chapter 1 only had the `初診料   291点` form, so stripping trailing points was enough, but
    chapter 2 has **kubun whose point column holds a formula**:

        Ｄ５００   薬剤     薬価が15円を超える場合は、薬価から15円を控除した額を…

    Squashing all whitespace would produce a file named `薬剤薬価が15円を超える場合は…`, so
    **after stripping the points, cut at the first run of 3+ spaces**.
    The 1–2 spaces left in a line by ruby removal (`膀胱 洗浄`) are squashed, not cut.
    """
    t = re.sub(r"[\s　]*[0-9０-９,，]+点\s*$", "", title).rstrip()
    t = re.split(r"[\s　]{3,}", t)[0]
    return re.sub(r"[\s　]+", "", t).strip()


RUBY_DROPPED = 0


def clean(lines: list[str], *, drop_ruby: bool) -> str:
    """Drop page footers, form feeds and ruby lines, and trim the common indent.

    **Indentation is preserved**. The fee schedule expresses hierarchy (注1 / イ / (1)) by
    column alignment, so only the left margin is normalised.
    """
    out = []
    for line in lines:
        line = line.replace("\x0c", "")
        if FOOTER_RE.match(line):
            continue
        if drop_ruby and RUBY_RE.match(line):
            global RUBY_DROPPED
            RUBY_DROPPED += 1
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


def _ctx_path(ctx: dict) -> tuple:
    """ctx as a tuple ordered part/section/sub-section/group (for hierarchy identity and dir names)."""
    return tuple((lv,) + ctx[lv] for lv in _LEVELS if lv in ctx)


def _ts_key(ctx: dict) -> tuple:
    """Identity key of a general-provisions (通則) block. **Unlike `_ctx_path`, includes the chapter**.

    `_ctx_path` holds only part and below, so chapter-only provisions (head of tsuchi chapter 2,
    chapter 3) and the all-chapter preamble at the head of the tsuchi (no chapter at all)
    **collapse into the same empty tuple and mix**. Prefix the chapter to keep them apart.
    """
    # **Chapter by number only**. The chapter name differs between the source (`特掲診療料`) and
    # the build-side label (`第2章 特掲診療料`); matching on the name would break
    head = (("章", ctx["章"][0]),) if "章" in ctx else ()
    return head + _ctx_path(ctx)


def ctx_label(ctx: dict) -> str:
    """Form `第10部 手術 第1節 手術料 第9款 腹部 （胃、十二指腸）`. The front-matter `section`."""
    out = []
    for lv in _LEVELS:
        if lv not in ctx:
            continue
        num, name = ctx[lv]
        out.append(f"（{name}）" if lv == "群" else f"第{num}{lv} {name}")
    return " ".join(out)


def ctx_label_below(ctx: dict, here: dict) -> str:
    """Label for only the levels deeper than `here` (`第1節 エックス線診断料`).

    Used when provisions of a level without its own directory are stacked on an ancestor, so
    the heading states **which level's provisions** they are. Empty string for the same level.
    """
    out = []
    for lv in _LEVELS:
        if lv not in ctx or here.get(lv) == ctx[lv]:
            continue
        num, name = ctx[lv]
        out.append(f"（{name}）" if lv == "群" else f"第{num}{lv} {name}")
    return " ".join(out)


# Line-head `別添` / `別表` = the body (chapters 1–4) has ended and the attached forms / tables begin.
# **They also appear before the body** (kokuji L1 `別表第一` / tsuchi L1 `別添１` are both body titles),
# so only apply after at least one chapter heading has been seen.
ANNEX_RE = re.compile(r"^\x0c*別[添表]")


def scan(path: Path, *, drop_ruby: bool) -> tuple[dict[str, dict], dict[tuple, dict], int]:
    """Cut the fee-schedule text by kubun number. Returns (kubun, provisions, number of shared bodies).

    "Next kubun anchor" alone is not a sufficient cut. Between kubun there are section changes and
    `＜通則＞` blocks; swallowing them **mixes the next section's provisions into the previous
    kubun's body** (e.g. Ａ００２ 外来診療料 would end with the part-2 inpatient provisions). The
    current kubun is closed as soon as a heading, a provisions marker or a `区分` line is hit.

    **Provisions are kept, not discarded.** In chapter 2 the surgery provisions (after-hours add-on,
    facility standards), test provisions etc. hold clauses essential to billing; dropping them is a
    silent loss. They go into the body of the hierarchy (part/section/sub-section/group) directory node.

    **Blocks without a `＜通則＞` anchor are picked up too.** The source also uses the form "clauses
    follow directly after a section heading" (imaging section 1 「エックス線診断に係る一般的事項」,
    58 lines; head-trauma CT add-on ア〜カ in the CT section, etc.). Keep accumulating as provisions
    after a heading line, and close on a kubun anchor, a `区分` line or the next heading. No block
    is created if nothing accumulates.
    """
    lines = path.read_text(encoding="utf-8").split("\n")
    out: dict[str, dict] = {}
    tsusoku: dict[tuple, dict] = {}
    cur: str | None = None
    cur_ts: tuple | None = None
    ctx: dict[str, tuple[int, str]] = {}
    aliases: list[tuple[str, str, int]] = []  # (alias, host, line)
    seen_chapter = False
    annex = False
    prev_head: tuple | None = None  # (level, number) when the previous non-blank line was a heading

    def open_ts(key: tuple, i: int) -> None:
        """Create the provisions block **only once content arrives** (no blocks for empty headings)."""
        tsusoku.setdefault(key, {"ctx": dict(ctx), "lines": [], "line_no": i + 1})

    for i, line in enumerate(lines):
        m = HEADING_RE.match(line)
        # **Two consecutive headings with the same level and number: the second is not a heading.**
        # One case in the tsuchi (`第３款 神経系・頭蓋` is followed by
        # `第３款 神経系・頭蓋の手術において神経内視鏡を…係る費用`, which is just the first
        # sentence of the sub-section's provisions wrapped in the shape of its name). Eating it
        # as a heading leaves the provisions as a fragment starting 「は、当該手術の…」
        if m and prev_head == (m.group(2), int(m.group(1).translate(_Z2H))):
            m = None
        if line.strip():
            prev_head = (
                (m.group(2), int(m.group(1).translate(_Z2H))) if m else None
            )
        if m:
            level = m.group(2)
            num = int(m.group(1).translate(_Z2H))
            ctx[level] = (num, re.sub(r"[\s　]+", " ", m.group(3).strip()))
            for lower in _LOWER[level]:
                ctx.pop(lower, None)
            if level == "章":
                seen_chapter = True
                annex = False
            cur = None
            cur_ts = _ts_key(ctx)
            continue
        # Once the body (chapters 1–4) ends and the attachments / appendix tables begin, pick up nothing.
        # **The tsuchi ends with 別添1の2 (provisions for the forms) and 別表1**; without closing,
        # the chapter 3 part 2 provisions swallow 別表1 (500 lines that pdftotext split column-wise)
        if seen_chapter and ANNEX_RE.match(line):
            annex = True
            cur = cur_ts = None
            continue
        if annex:
            continue
        m = GROUP_RE.match(line)
        if m and not re.search(r"[0-9０-９]", m.group(1)):
            # Treat as a heading if the next real line (skipping ruby / blanks) is an anchor or provisions marker
            j = i + 1
            while j < len(lines) and (
                not lines[j].strip() or RUBY_RE.match(lines[j])
            ):
                j += 1
            if j < len(lines) and (
                ANCHOR_RE.match(lines[j]) or TSUUSOKU_RE.match(lines[j])
            ):
                # Groups have no number in the kokuji. **Keep it fixed at 0** — counting separately
                # in kokuji and tsuchi makes ctx_path diverge and the provisions matching fails
                ctx["群"] = (0, re.sub(r"[\s　]+", "", m.group(1)))
                cur = None
                cur_ts = _ts_key(ctx)
                continue
        if TSUUSOKU_RE.match(line) or KUBUN_RE.match(line):
            cur = None
            # **A `区分` line ends the provisions**. `＜通則＞` reopens provisions at the current level
            cur_ts = _ts_key(ctx) if TSUUSOKU_RE.match(line) else None
            continue
        m = ANCHOR_RE.match(line)
        if m:
            cur_ts = None
            key = kubun_key(m.group(1))
            if key in out:
                sys.exit(f"{path}: anchor for kubun {key} appeared twice (line {i + 1})")
            cur = key
            head = re.sub(r"\s+", " ", m.group(2).strip())
            out[key] = {
                "raw": m.group(1),
                "head": head,
                "title": strip_points(m.group(2)),
                "ctx": dict(ctx),
                "ctx_path": _ctx_path(ctx),
                "lines": [line],
                "line_no": i + 1,
                "drop_ruby": drop_ruby,
            }
            # **Two or more kubun headings may share one line** (26 lines in the tsuchi):
            #     Ｋ０００   創傷処理、Ｋ０００－２   小児創傷処理
            # A naive cut drops the whole Ｋ０００－２ entry (silent loss).
            # Give the following body to both. **The line-head anchor wins**; aliases apply
            # only to kubun that have no anchor of their own (some, like K783, appear
            # in both forms)
            for tok in KUBUN_TOKEN_RE.findall(head):
                aliases.append((kubun_key(tok), key, i + 1))
            continue
        if cur:
            out[cur]["lines"].append(line)
        elif cur_ts is not None:
            if cur_ts not in tsusoku and (
                not line.strip() or (drop_ruby and RUBY_RE.match(line))
            ):
                # Do not open a block on blank / ruby lines right after a heading. The table of
                # contents has ruby lines too; opening there makes body provisions "start at a TOC line"
                continue
            open_ts(cur_ts, i)
            tsusoku[cur_ts]["lines"].append(line)

    # **A heading-only kubun "shares its body with the next kubun"**. The tsuchi sometimes
    #     Ａ２３８－６     精神科救急搬送患者地域連携紹介加算
    #     Ａ２３８－７     精神科救急搬送患者地域連携受入加算
    #      (１) 精神科救急搬送患者地域連携紹介加算及び…は、…
    # raises **one entry for two kubun** like this. A naive cut leaves the former as an
    # empty node with a single heading line and loses its requirements entirely (silent loss).
    # If a heading is immediately followed by the next heading, give the body to both.
    # `削除` (deleted) headings correctly have no body, so they are left out.
    order = sorted(out, key=lambda k: out[k]["line_no"])
    carry: list[str] | None = None
    carry_line: int | None = None
    shared = 0
    for key in reversed(order):
        node = out[key]
        content = [
            ln
            for ln in node["lines"][1:]
            if ln.strip() and not (drop_ruby and RUBY_RE.match(ln))
        ]
        adjacent = carry_line is not None and carry_line == node["line_no"] + 1
        # **If the heading line carries points, the kubun is complete in one line.**
        # Kokuji chapter 2 has many of these (`Ｄ００６－７ …遺伝子多型  2,004点` lined
        # up back to back); without checking for points, the next kubun's body is pulled
        # in by chain (measured: 597 kubun across all chapters wrongly marked as shared)
        self_contained = bool(SELF_POINT_RE.search(node["lines"][0]))
        if (
            not content
            and not self_contained
            and node["title"] != "削除"
            and carry is not None
            and adjacent
        ):
            node["lines"] = node["lines"] + carry
            node["shared"] = True
            shared += 1
        else:
            node["shared"] = False
        carry, carry_line = node["lines"], node["line_no"]

    for alias, host, line_no in aliases:
        if alias in out:
            continue
        h = out[host]
        out[alias] = {
            **h,
            "raw": alias,
            "title": strip_points(h["head"]),
            "shared": True,
            "shared_inline": host,
            "line_no": line_no,
        }

    for v in out.values():
        v["body"] = clean(v["lines"], drop_ruby=v["drop_ruby"])
    for v in tsusoku.values():
        v["body"] = clean(v["lines"], drop_ruby=drop_ruby)
    # Levels whose heading is directly followed by the next heading (TOC etc.) have no content. Keep them out of the count
    tsusoku = {k: v for k, v in tsusoku.items() if v["body"].strip()}
    return out, tsusoku, shared


# ---------------------------------------------------------------------------
# Master
# ---------------------------------------------------------------------------

C_CODE, C_SHORT, C_IDENT, C_POINT = 2, 4, 10, 11
C_ALPHA, C_CHANGED, C_ABOLISHED = 84, 86, 87
C_CHAPTER, C_PART, C_KUBUN, C_BRANCH, C_SEQ = 89, 90, 91, 92, 93
C_FULLNAME = 112

# Point kind (item 11). layout_rec1.pdf appendix 7-1
IDENT_FMT = {
    "1": "{}円",
    "3": "{}点",
    "4": "{}点 (購入価格)",
    "5": "{}%加算",
    "6": "{}%減算",
    "7": "{}点 (減点)",
    "8": "{}点減算",
}


def fmt_point(ident: str, raw: str) -> str:
    try:
        v = float(raw)
    except ValueError:
        return raw
    num = f"{int(v):,}" if v == int(v) else f"{v:,}"
    fmt = IDENT_FMT.get(ident)
    if fmt is None:
        sys.exit(f"unknown point kind {ident!r} (check layout_rec1.pdf appendix 7-1)")
    return fmt.format(num)


def row_name(r: list[str]) -> str:
    """Name shown in the table. **One row has a trailing full-width space**, so strip it.

    Otherwise an invisible difference enters the markdown table and verify's matching
    reports "name differs" (the source value is the same).
    """
    return (r[C_FULLNAME] or r[C_SHORT]).replace("|", "｜").strip()


def master_rows() -> list[list[str]]:
    path = CACHE / "masters" / "s.csv"
    rows = []
    with open(path, encoding="cp932", newline="") as f:
        for row in csv.reader(f):
            if len(row) < 150:
                sys.exit(f"{path}: column count differs from the expected 150: {len(row)}")
            rows.append(row)
    return rows


def load_master(
    rows: list[list[str]], chapter: str, *, kokuji: dict[str, dict] | None = None
) -> tuple[dict[str, list[list[str]]], list[list[str]]]:
    """Group rows with chapter == chapter by kubun key. Returns (rows per kubun, rows with no kubun).

    **Kubun `000` + branch `00` + item `000` is the "not tied to any kubun" address**, and it
    collides with `Ａ０００` 初診料 (whose kubun number really is 000). 22 rows in chapter 1 and
    129 in chapter 2 qualify. Grouping naively inflates the A000 code table from 41 to 63 rows and
    **mixes re-visit add-ons into the first-visit node**.

    However, **in chapter 2 real kubun also live at this address**: `Ｃ０００` 往診料 720点,
    `Ｄ０００` 尿中一般物質定性半定量検査 26点, `Ｅ０００` 透視診断 110点 and
    `Ｇ０００` 皮内、皮下及び筋肉内注射 25点 — four rows. Discarding them makes
    **urinalysis and fluoroscopy vanish from the corpus**, so
    **only rows whose name exactly matches the kokuji `X000` heading** are rescued
    (active only when `kokuji` is passed; the master offers no clue other than the name).

    Chapters whose alphabet part is `－` (ch3 / forensic / meals) or `＊` (procedures with no
    kubun number) **collide across parts on the kubun number alone**
    (ch3 has kubun 001 in both part 1 and part 2). **Include the part in the key**.
    """
    groups: dict[str, list[list[str]]] = defaultdict(list)
    unassigned: list[list[str]] = []
    rescue = {}
    if kokuji:
        for a in "BCDEFGHIJKLMNO":
            k = f"{a}000"
            if k in kokuji:
                rescue[strip_points(kokuji[k]["head"])] = k
    for row in rows:
        if row[C_CHAPTER] != chapter:
            continue
        alpha, branch = row[C_ALPHA], row[C_BRANCH]
        named = alpha.isalpha()
        if row[C_KUBUN] == "000" and branch == "00" and row[C_SEQ] == "000":
            hit = rescue.get(row_name(row)) if named else None
            if hit is None:
                unassigned.append(row)
                continue
            groups[hit].append(row)
            continue
        head = alpha if named else f"P{int(row[C_PART])}-"
        key = head + row[C_KUBUN] + ("" if branch == "00" else f"-{int(branch)}")
        groups[key].append(row)
    return dict(groups), unassigned


# ---------------------------------------------------------------------------
# Output building blocks
# ---------------------------------------------------------------------------

_FS_BAD = re.compile(r'[/\\:*?"<>|\x00-\x1f]')


def fs_safe(s: str) -> str:
    # The name becomes a URL path: space → `_`, URL-breaking ASCII → full-width (textfix.safe_segment)
    return safe_segment(_FS_BAD.sub("・", s))


def slug(key: str, title: str) -> str:
    return f"{key}_{fs_safe(title)}"


def yaml_str(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


# ---------------------------------------------------------------------------
# Pointers to neighbouring sections (facility-standard notice / Q&A / this hospital's registrant list)
# ---------------------------------------------------------------------------

# `kubun` in the registrant list is the source's kubun cell verbatim (`A207 1` / `A000 注1,A002 注2` /
# `K 通則19,K475` / `歯科M029 注3`). Pick up only the kubun-number text. **Not right after kanji / letters**
# (so `歯科M029` is not read as M029). `K 通則19` is not a kubun number and does not match
_KUBUN_TOKEN_RE = re.compile(r"(?<![A-Za-z\u3040-\u9fff])[A-O]\d{3}(?:-\d+){0,2}")
_FM_KUBUN_RE = re.compile(r"^kubun: '?([^'\n]*)'?$", re.M)


def _front_kubun(md: Path) -> str:
    """`kubun` from the front matter (empty if absent). Reads only the first 1,500 chars."""
    head = md.read_text(encoding="utf-8")[:1500]
    m = _FM_KUBUN_RE.search(head.split("\n---", 2)[0] if head.startswith("---") else "")
    return m.group(1).strip() if m else ""


def _doc_path(md: Path) -> str:
    """Real file → canonical form written in bodies (`_index.md` means its directory)."""
    rel = md.relative_to(Path("data/doc"))
    if md.name == "_index.md":
        return dir_ref(rel.parent.as_posix())
    return leaf_ref(rel.with_suffix("").as_posix())


class Xref:
    """Kubun number → node path in a neighbouring section (canonical form). Built by scanning the doc tree at build time."""

    def __init__(self) -> None:
        self.qa: dict[str, str] = {}
        self.kijun: dict[str, list[str]] = defaultdict(list)
        self.touroku: dict[str, list[str]] = defaultdict(list)
        self.present = {"qa": False, "kijun": False, "touroku": False, "dpc": False}

    def load(self) -> "Xref":
        qa_dir = CATEGORY / SEC_QA
        if qa_dir.is_dir():
            self.present["qa"] = True
            for md in sorted(qa_dir.rglob("*.md")):
                if md.name == "_index.md":
                    continue
                k = _front_kubun(md)
                if k and k != "-":
                    self.qa[k] = _doc_path(md)
        for sec in (SEC_KIJUN_CH1, SEC_KIJUN_CH2):
            d = CATEGORY / sec
            if not d.is_dir():
                continue
            self.present["kijun"] = True
            for md in sorted(d.rglob("*.md")):
                # Pick up `_index.md` only for "第N split into items" parents (parent dir name starts with `第`).
                # `_index.md` of attachment / part / sub-section dirs has a kubun range, so skip
                if md.name == "_index.md" and not md.parent.name.startswith("第"):
                    continue
                for k in _front_kubun(md).split(","):
                    k = k.strip()
                    if k and _KUBUN_TOKEN_RE.fullmatch(k):
                        self.kijun[k].append(_doc_path(md))
        if (CATEGORY / SEC_DPC).is_dir():
            self.present["dpc"] = True
        if TOUROKU_DIR.is_dir():
            self.present["touroku"] = True
            for md in sorted(TOUROKU_DIR.rglob("*.md")):
                if md.name == "_index.md":
                    continue
                for k in dict.fromkeys(_KUBUN_TOKEN_RE.findall(_front_kubun(md))):
                    self.touroku[k].append(_doc_path(md))
        return self

    def summary(self) -> str:
        return (
            f"xref: qa {'yes' if self.present['qa'] else 'no'} ({len(self.qa)} kubun) / "
            f"kijun {'yes' if self.present['kijun'] else 'no'} ({len(self.kijun)} kubun, "
            f"{sum(len(v) for v in self.kijun.values())} nodes) / "
            f"touroku {'yes' if self.present['touroku'] else 'no'} ({len(self.touroku)} kubun, "
            f"{sum(len(v) for v in self.touroku.values())} nodes) / "
            f"DPC {'yes' if self.present['dpc'] else 'no'}"
        )


XREF = Xref()  # loaded in main(). Before load, only the section pointer is emitted


def _paths(paths: list[str], grep_at: str) -> str:
    shown = " / ".join(f"`{p}`" for p in paths[:XREF_LIST_MAX])
    if len(paths) > XREF_LIST_MAX:
        shown += f" — ほか {len(paths) - XREF_LIST_MAX} 本は `{grep_at}` を名称で grep"
    return shown


def dpc_note(section: str | None = None) -> str:
    """The one DPC line added to inpatient-fee leaves.

    This hospital is a DPC hospital, so the inpatient fee of a bundled-payment patient on a general
    ward is not the daily sum of this kubun's points. Stops a model that read an inpatient-fee leaf
    (chapter 1 part 2) from building the bill as "basic inpatient fee × days", and sends it to the
    bundled-points section (`{{DOC_BASE}}/診療報酬/DPC点数表R08/`). **What is bundled and what is
    fee-for-service is not asserted here** (some add-ons stay fee-for-service, some are replaced by
    coefficient I) — make it read the tsuchi 第3.
    """
    return (
        f"> - **包括払い (DPC/PDPS) の入院ではこの点数を日ごとに足さない**: 当院は DPC 対象病院で、"
        f"診断群分類点数表に該当する一般病棟の患者は `{dir_ref(f'/診療報酬/{SEC_DPC}')}` の 1 日あたり包括点数 × "
        "医療機関別係数で算定し、入院基本料・入院基本料等加算の多くは包括点数か機能評価係数Ⅰに置き換わる "
        "(この区分が包括されるか・出来高で別に算定できるか・係数Ⅰに置き換わるかは "
        f"`{dir_ref(f'/診療報酬/{SEC_DPC}/00_DPC留意事項通知')}` の 第3 と "
        f"`{leaf_ref(f'/診療報酬/{SEC_DPC}/00_北大病院の係数')}` で確かめる)。"
        "DPC 対象外の患者・包括対象外の 14 桁・入院日Ⅲを超えた日はこの点数表 (出来高) で算定する"
        + (
            # Specific inpatient fees (ICU/HCU etc.) have a separate daily add-on table in the
            # billing notification even for DPC patients, distinct from this kubun's points.
            # Stops a model reading this from adding the fee-for-service points
            f"。**特定入院料は DPC 患者では日額の加算点数に置き換わる** (この区分の点数ではない): "
            f"`{leaf_ref(f'/診療報酬/{SEC_DPC}/00_特定入院料のDPC加算')}` (算定告示 項 4〜14、当院は項 4 = 特定機能病院) と "
            f"`{leaf_ref(f'/診療報酬/{SEC_DPC}/00_算定方法')}`"
            if section and "特定入院料" in section else ""
        )
    )


def scope_note(key: str | None = None, section: str | None = None) -> str:
    """The "neighbouring sections" pointer placed at the top of a fee-schedule node.

    **Stops a model that read a single node from asserting "no facility standard / no Q&A" or
    "not in the fee schedule = not billable".** read returns only one node, so writing it in the
    section `_index.md` alone does not reach (his_faq made the same mistake). Leaves with a known
    kubun number name the **real path** of the matching node; when none exists, state why and
    the next step — so that "no matching node" is not read as "no facility standard / no Q&A /
    not filed" (`20_shisetsu_kijun.md`, "what the registrant list really is").

    If `section` (chapter-1 front-matter `section`) is inpatient fees, add the DPC line (`dpc_note`).
    """
    head = (
        "> **収録範囲の案内**: このノードは点数表 (告示・留意事項通知・診療行為コード) だけ。"
        "「別に厚生労働大臣が定める施設基準」の中身・疑義解釈・当院の届出状況は**別のセクション**にある。"
        "「当院で算定できるか」は **点数表 (ここ) → 施設基準 → 当院の登録状況** の順に 3 つ read して"
        "材料を示すところまでにする (届出の有無そのものは登録者一覧でも分からない — 医事課施設基準係へ)。"
        "点数表だけ・施設基準だけで算定可否を断定しないこと。"
    )
    if key is None:
        return "\n".join(
            [
                head,
                f"> - 施設基準の中身 (施設基準通知): `{dir_ref(f'/診療報酬/{SEC_KIJUN_CH1}')}` (第1章) / "
                f"`{dir_ref(f'/診療報酬/{SEC_KIJUN_CH2}')}` (第2章)。各区分ノードが対応ノードの path を名乗る",
                f"> - 疑義解釈 (Q&A): `{dir_ref(f'/診療報酬/{SEC_QA}')}`。**{QA_AS_OF}**を収録 — "
                f"それ以降の その N は未収録で、{QA_LATEST_HINT} で確認すること",
                f"> - 当院の登録状況: `{dir_ref(TOUROKU_DOC)}` (施設基準登録者一覧・{TOUROKU_AS_OF})。"
                "**専任・専従で登録している職員の名簿**であって届出の有無そのものではない",
            ]
        )
    kijun_sec = SEC_KIJUN_CH1 if key.startswith("A") else SEC_KIJUN_CH2
    lines = [head]
    hits = XREF.kijun.get(key, [])
    if hits:
        lines.append(
            f"> - 施設基準の中身 (施設基準通知{f'、{len(hits)} 本' if len(hits) > 1 else ''}): "
            + _paths(hits, dir_ref(f"/診療報酬/{kijun_sec}"))
        )
    else:
        lines.append(
            f"> - 施設基準の中身 (施設基準通知): この区分番号に結び付いたノードは無い "
            "(施設基準通知と点数表は**名称**で突き合わせており、取りこぼしがある)。告示の注に"
            "「別に厚生労働大臣が定める施設基準」とあれば施設基準はあるので、"
            f"名称で `{dir_ref(f'/診療報酬/{kijun_sec}')}` を grep してから答えること"
        )
    qa = XREF.qa.get(key)
    if qa:
        extra = ""
        if "/O_その他/" in qa:
            extra = (
                "。**ベースアップ評価料・看護職員処遇改善評価料の問は最初に出た評価料の区分にしか"
                f"置かれていない** — `{dir_ref(f'/診療報酬/{SEC_QA}/O_その他')}` の 4 ノードを全部 read するか grep すること"
            )
        lines.append(
            f"> - 疑義解釈 (Q&A、**{QA_AS_OF}**を収録): `{qa}`{extra}。"
            f"それ以降の その N は未収録 — {QA_LATEST_HINT} で確認すること"
        )
    else:
        lines.append(
            f"> - 疑義解釈 (Q&A、**{QA_AS_OF}**を収録): この区分番号を名指しした問は無い。"
            f"区分番号を書かない問は `{dir_ref(f'/診療報酬/{SEC_QA}/区分番号なし')}` に原本の見出しで束ねてあり、"
            f"他の区分の問の中で触れられていることもあるので、名称で `{dir_ref(f'/診療報酬/{SEC_QA}')}` を grep "
            f"してから「疑義解釈は無い」と言うこと。それ以降の その N は未収録 — {QA_LATEST_HINT} で確認"
        )
    tr = XREF.touroku.get(key, [])
    if tr:
        lines.append(
            f"> - 当院の登録状況 (施設基準登録者一覧・{TOUROKU_AS_OF}): "
            + _paths(tr, dir_ref(TOUROKU_DOC))
            + "。**専任・専従で登録している職員の名簿**であって届出の有無そのものではない "
            f"(登録があっても届出済みとは限らない = 準備中を含む)。最新は {TOUROKU_CONTACT} へ"
        )
    else:
        lines.append(
            f"> - 当院の登録状況 (施設基準登録者一覧・{TOUROKU_AS_OF}): この区分番号は一覧に載っていない。"
            "**載っていない = 届け出ていない、ではない** (一覧は専任・専従の職員の名簿で、"
            f"職員配置を要しない施設基準は載らない)。届出の有無は {TOUROKU_CONTACT} へ"
        )
    if XREF.present["dpc"] and section and section.startswith(DPC_SECTION_PREFIX):
        lines.append(dpc_note(section))
    return "\n".join(lines)

CHILD_FIELDS = [
    "fields:",
    "  kubun: str",
    "  section: str",
    "  source?: str",
    "  retrieved_at?: date",
]


def front_matter(
    title: str, key: str, section: str, *, child_fields: bool, source: str = SOURCE
) -> list[str]:
    """Front matter shared by all nodes.

    `fields:` is added only when `child_fields=True` (a directory node declares the closed
    schema of its children). `title` and `fields` are reserved keys of the walker, so
    **they are not validated against the parent's schema** — the directory node itself
    satisfies kubun / section / source? / retrieved_at? as is.
    """
    fm = [
        "---",
        f"title: {yaml_str(title)}",
        f"kubun: {yaml_str(key)}",
        f"section: {yaml_str(section)}",
        f"source: {source}",
        f"retrieved_at: {yaml_str(RETRIEVED)}",
    ]
    if child_fields:
        fm += CHILD_FIELDS
    return fm + ["---", ""]


# The master's 「変更年月日」 (item 87) is not necessarily the revision date. It is **the day the row
# last changed**, and the R8 edition has 302 rows dated after the revision (e.g. 入院ベースアップ評価料
# 251〜500 effective 2027-06-01, not yet in force). **Writing "from 2026-06-01" on every row would be
# a lie**, so nodes with rows that differ from the default name the dates
DEFAULT_CHANGED = "20260601"
DEFAULT_ABOLISHED = "99999999"


def _date(v: str) -> str:
    return f"{v[:4]}-{v[4:6]}-{v[6:]}" if len(v) == 8 else v


def code_table(rows: list[list[str]], *, master_hint: bool = True) -> list[str]:
    """Markdown table of procedure codes (without the section heading)."""
    out = ["| 診療行為コード | 名称 | 点数 |", "|---|---|---|"]
    for r in sorted(rows, key=lambda r: r[C_CODE]):
        out.append(f"| {r[C_CODE]} | {row_name(r)} | {fmt_point(r[C_IDENT], r[C_POINT])} |")
    out.append("")
    odd = sorted(
        {
            (r[C_CHANGED], r[C_ABOLISHED])
            for r in rows
            if (r[C_CHANGED], r[C_ABOLISHED]) != (DEFAULT_CHANGED, DEFAULT_ABOLISHED)
        }
    )
    if not odd:
        out += [f"全行が {APPLIES_FROM} 適用開始・廃止日なし (現行)。", ""]
    else:
        n = sum(
            1
            for r in rows
            if (r[C_CHANGED], r[C_ABOLISHED]) != (DEFAULT_CHANGED, DEFAULT_ABOLISHED)
        )
        out += [
            f"**この表は変更年月日が {APPLIES_FROM} でない行を {n} 件含む** "
            + "・".join(f"変更年月日 {_date(c)} / 廃止年月日 {_date(a)}" for c, a in odd)
            + "。マスターの「変更年月日」は**その行が最後に変わった日**で、"
            "改定日より後の日付は**その日から効く行**(まだ算定できない)。"
            "どの行がそれに当たるかはマスター CSV の項番87・88 を引くこと。",
            "",
        ]
    if master_hint:
        out += [
            f"**コードを網羅的に絞り込む・数値条件で引く**なら、この表ではなく"
            f"マスター CSV の原本を sandbox に取り込んで引く: `{MASTER_CSV_URL}`"
            f" ({MASTER_CSV_DESC})。",
            "",
        ]
    return out


# ---------------------------------------------------------------------------
# Node generation for chapters 1 / 2 (kokuji + tsuchi + code table)
# ---------------------------------------------------------------------------


class Section:
    """Output directory for one section and its absolute path in the doc tree."""

    def __init__(self, name: str):
        self.dir = CATEGORY / name
        self.doc = "/" + "/".join(self.dir.parts[2:])


BLOCK_KOKUJI = "告示"
BLOCK_TSUCHI = "留意事項通知"
# A kokuji / tsuchi block above this many chars goes to a child node.
# The measured distribution (chapters 1–2) has a **clear valley**, and the line sits in it:
#   kokuji … 31,737 (B001) / 24,200 (O100) / 18,249 (A400) / 15,423 (O003) …
#   tsuchi … 53,225 (B001) / 22,753 (D023) / 21,307 (C005 and C005-1-2) / 15,566 (D012) …
# At 20,000 **nothing in chapter 1 is split** (max is A400's kokuji 18,249 and
# A000's tsuchi 15,052). This also keeps chapter 1's existing paths unchanged.
BLOCK_SPLIT = 20_000

KOKUJI_LEAD = (
    "「診療報酬の算定方法」(平成20年厚生労働省告示第59号) 別表第一 医科診療報酬点数表 — "
    "令和8年厚生労働省告示第69号による改正後。**点数と「注」の算定要件はここが正典**。"
)
TSUCHI_LEAD = (
    "「診療報酬の算定方法の一部改正に伴う実施上の留意事項について」"
    "(令和8年3月5日 保医発0305第6号) 別添1 医科診療報酬点数表に関する事項。"
)


def child_md(
    *,
    key: str,
    node: dict,
    label: str,
    long: str,
    heading: str,
    lead: str,
    block: list[str],
    parent_doc: str,
    chapter_label: str,
) -> str:
    """Child node holding a single block of a kubun (kokuji / tsuchi / code table).

    **The body states that the counterpart lives in the parent.** /doc/read returns one node,
    so a model that read only this child must not stop at "these are the points, requirements unknown".
    """
    section = node["section"]
    body = [
        f"{TENSUHYO} {chapter_label} {section} 「{node['raw']} {node['title']}」({key}) "
        f"の**{long}**。大きいので区分本体から分けてある。",
        "",
        f"**この区分の残り (告示の点数と注 / 留意事項通知の算定要件 / 診療行為コード表の"
        f"うちここに無いもの) は親ノードとその兄弟にある** — `{parent_doc}` を read して"
        f"から、そこが名乗る子を辿ること。ここには{long}しか入っていない。",
        "",
        scope_note(key, section),
        "",
        f"## {heading}",
        "",
        lead,
        "",
    ] + block
    return "\n".join(
        front_matter(f"{key} {node['title']} — {long}", key, section, child_fields=False)
        + body
    )


def leaf_md(
    key: str, node: dict, dir_doc: str, chapter_label: str
) -> tuple[str, dict[str, str]]:
    """Build the files for one kubun. Returns (slug, {relative path: body}).

    If the blocks are small, a single node `{"<slug>.md": ...}`. **Only large blocks go to
    child nodes** — code table above `CODE_TABLE_SPLIT` rows, kokuji / tsuchi above
    `BLOCK_SPLIT` chars. Then `<slug>/_index.md` + `<slug>/<block>.md`.

    **This is not one node split into N files; it becomes N nodes.**
    The parent always names the child paths and the child names the parent (`verify` checks both ways).
    """
    kok, tsu, rows = node["kokuji"], node["tsuchi"], node["rows"]
    title = node["title"]
    section = node["section"]
    name = slug(key, title)
    # The body uses the **full-width kubun number as in the source** (`Ａ０００`) so a model
    # can grep with the source spelling too. Path / front matter use half-width (`A000`)
    raw = kok["raw"] if kok else key

    # --- Assemble each block's content first -------------------------------------------
    blocks: list[dict] = []

    kok_lines: list[str]
    if kok is None:
        kok_lines = [
            f"**この区分番号は告示に項が立っていない。** マスター (医科診療行為マスター) "
            f"にはコード表用番号 `{key}` の行があるが、点数表側に対応する区分が無い"
            f"(区分番号が 3 段の形でしか立っていない・レセ電側だけの番地、のいずれか)。"
            f"下のコード表と、同じ階層の隣接する区分ノードを併せて見ること。",
            "",
        ]
        kok_size = 0
    else:
        note = ""
        if kok.get("shared_inline"):
            note = (
                f"**この区分の見出しは告示で `{kok['shared_inline']}` と同じ行に"
                "並べて立てられており、本文を共有している。**\n"
            )
        kok_lines = ([note] if note else []) + ["```", kok["body"], "```", ""]
        kok_size = len(kok["body"])
    blocks.append(
        dict(
            heading="告示 (点数と注)",
            label=BLOCK_KOKUJI,
            long="告示 (点数と注)",
            lead=KOKUJI_LEAD,
            lines=kok_lines,
            size=kok_size,
        )
    )

    if tsu is None:
        tsu_lines = [
            "**この区分には留意事項通知の項が無い** "
            "(「診療報酬の算定方法の一部改正に伴う実施上の留意事項について」別添1 に "
            f"{key} の項が立っていない)。算定要件は上の告示の「注」と、"
            "施設基準通知 (上の「収録範囲の案内」の施設基準の項が名乗るノード、"
            "無ければそこに書いたセクションを名称で grep) を参照すること。"
            "**第2章では通知の項が無い区分のほうが多い** (手術など)。",
            "",
        ]
        tsu_size = 0
        tsu_lead = TSUCHI_LEAD
    else:
        note = ""
        if tsu.get("shared_inline"):
            note = (
                f" **この項は `{tsu['shared_inline']}` と同じ行に見出しが並べて立てられており、"
                "本文を共有している。**"
            )
        elif tsu.get("shared"):
            note = (
                " **この項は複数の区分に共通**で、原本では見出しが並べて立てられている "
                "(下のブロック冒頭の見出し行を参照)。"
            )
        tsu_lead = TSUCHI_LEAD + note
        tsu_lines = ["```", tsu["body"], "```", ""]
        tsu_size = len(tsu["body"])
    blocks.append(
        dict(
            heading="留意事項通知 (算定要件)",
            label=BLOCK_TSUCHI,
            long="留意事項通知 (算定要件)",
            lead=tsu_lead,
            lines=tsu_lines,
            size=tsu_size,
        )
    )

    covers = node.get("covers")
    cover_note = ""
    if covers:
        cover_note = (
            f" **この表は {'・'.join('`' + c + '`' for c in covers)} の分を含む。**"
            "マスターのコード表用番号が枝番 1 段までしか持たないため、"
            "3 段目の区分のコードもここに入っている。"
        )
    code_lead = (
        "医科診療行為マスター (診療報酬情報提供サービス、令和8年度版) の当該区分の全行。"
        "レセプトに載るコードで、告示の「注」加算もここに 1 行ずつ立っている。" + cover_note
    )
    lumped = node.get("lumped_at")
    if lumped:
        # Three-level kubun number (e.g. Ｂ００１－２－２). The master holds one branch level,
        # so the codes are lumped on the second-level node. **Name that path**
        code_lines = [
            f"この区分は区分番号が 3 段 (`{key}`) だが、**医科診療行為マスターの"
            f"コード表用番号は枝番 1 段までしか持たない**ため、この区分のコードは"
            f"`{node['lumped_key']}` 系列のコード表に他の区分と一緒に入っている。"
            f"レセプトコードが要るなら `{lumped}` を read すること "
            f"(マスター側に区分ごとの区切りが無いので、ここに切り出すことはできない)。",
            "",
        ]
        code_heading = "診療行為コードと点数 — **マスターでは親の枝番にまとまっている**"
        code_lead = ""
        rows_n = 0
    elif not rows:
        code_lines = [
            "**この区分にはマスターの行が 1 件も無い。** 薬剤料・特定保険医療材料料・"
            "フィルム・酸素のように**薬価基準や材料価格基準で額が決まる区分**は"
            "医科診療行為マスターに載らない (別のマスターにある)。"
            "算定の仕方は上の告示・通知を読むこと。",
            "",
        ]
        code_heading = "診療行為コードと点数 (0 件)"
        code_lead = ""
        rows_n = 0
    else:
        code_lines = code_table(rows)
        code_heading = f"診療行為コードと点数 ({len(rows)} 件)"
        rows_n = len(rows)
    blocks.append(
        dict(
            heading=code_heading,
            label=CODE_CHILD,
            long="診療行為コードと点数",
            lead=code_lead,
            lines=code_lines,
            size=0,
            rows=rows_n,
        )
    )

    # --- Decide which blocks go to children --------------------------------------------
    for b in blocks:
        b["split"] = (
            b.get("rows", 0) > CODE_TABLE_SPLIT
            if b["label"] == CODE_CHILD
            else b["size"] > BLOCK_SPLIT
        )
    split = any(b["split"] for b in blocks)

    body = [
        f"{TENSUHYO} {chapter_label} {section} 「{raw} {title}」({key})。"
        f"適用は {APPLIES_FROM} から。",
        "",
        # **Stops a model that read one node from asserting "not in the fee schedule = not billable".**
        # Also written in the section _index.md, but read returns one node, so it must be on
        # the leaf too or it never reaches (his_faq made the same mistake).
        # Names the real paths in neighbouring sections (facility standards / Q&A / registrant list) by kubun.
        # Inpatient fees also point to `{{DOC_BASE}}/診療報酬/DPC点数表R08/` (`dpc_note`)
        scope_note(key, section),
        "",
    ]
    files: dict[str, str] = {}
    for b in blocks:
        if not b["split"]:
            body += [f"## {b['heading']}", ""]
            if b["lead"]:
                body += [b["lead"], ""]
            body += b["lines"]
            continue
        # **The body states that a child exists.** The heading stays — so a model that only
        # skims headings still learns "it exists, but in another node"
        how = (
            f"コードが {b['rows']} 件と多い"
            if b["label"] == CODE_CHILD
            else f"本文が {b['size']:,} 字と大きい"
        )
        body += [
            f"## {b['heading']} — **子ノードにある**",
            "",
            f"この区分は{how}ため、**{b['long']}は子ノードに分けてある。"
            f"このノードには載っていない。** "
            f"`{leaf_ref(f'{dir_doc}/{name}/{b['label']}')}` を read すること (消えたわけではない)。",
            "",
        ]
        files[f"{name}/{b['label']}.md"] = child_md(
            key=key,
            node={**node, "raw": raw},
            label=b["label"],
            long=b["long"],
            heading=b["heading"],
            lead=b["lead"],
            block=b["lines"],
            parent_doc=dir_ref(f"{dir_doc}/{name}"),
            chapter_label=chapter_label,
        )

    if split:
        files[f"{name}/_index.md"] = "\n".join(
            front_matter(title, key, section, child_fields=True) + body
        )
        return name, files
    return name, {
        f"{name}.md": "\n".join(
            front_matter(title, key, section, child_fields=False) + body
        )
    }


# ---------------------------------------------------------------------------
# Building the hierarchy (chapter 2)
# ---------------------------------------------------------------------------


def dir_name(level: str, num: int, name: str, seq: int) -> str:
    """File name of a hierarchy directory. **/doc/list sorts by name**, so the number leads.

    Sections / sub-sections use the kokuji number zero-padded to 2 digits (`第01款_皮膚・皮下組織`).
    Without padding `第10款` sorts before `第1款`.
    Groups have no number in the kokuji, so **we number them by order of appearance**
    (`群01_皮膚、皮下組織`). Not a kokuji number.
    """
    if level == "群":
        return f"群{seq:02d}_{fs_safe(name)}"
    return f"第{num:02d}{level}_{fs_safe(name)}"


def collapse(keys: list[str], nodes: dict, depth: int) -> tuple[list[str], dict]:
    """Split `keys` into "kubun placed directly here" and "child directories".

    **No split while the child count is at or below CHILD_LIMIT** (shallowest form). Only when
    exceeded, dig one level at the next kokuji division (part/section/sub-section/group at `depth`).
    Returns (direct kubun, {dir key: (ctx, contents)}).
    """
    if len(keys) <= CHILD_LIMIT or depth >= len(_LEVELS):
        return keys, {}
    here: list[str] = []
    sub: dict[tuple, list[str]] = {}
    for k in keys:
        p = nodes[k]["ctx_path"]
        if len(p) <= depth:
            here.append(k)
        else:
            sub.setdefault(p[: depth + 1], []).append(k)
    if not sub:
        return keys, {}
    out = {}
    for i, p in enumerate(
        sorted(sub, key=lambda p: min(nodes[k]["order"] for k in sub[p]))
    ):
        lv, num, name = p[-1]
        out[dir_name(lv, num, name, i + 1)] = (p, sub[p])
    return here, out


# ---------------------------------------------------------------------------
# Index text
# ---------------------------------------------------------------------------

CATEGORY_INDEX = f"""---
title: 診療報酬
fields:
  tensuhyo: str
  publisher: str
  applies_from: date
  source?: str
  retrieved_at?: date
---

公的医療保険の診療報酬 (保険点数) の一次資料を収容する。厚生労働省が告示・通知した
点数表そのものと、レセプト電算処理システムの基本マスターを突き合わせたもの。

## セクションの分かれ方

- `医科点数表R08_基本診療料` … 第1章 基本診療料 (初診・再診・入院料等)
- `医科点数表R08_特掲診療料` … 第2章 特掲診療料 (医学管理 B / 在宅 C / 検査 D /
  画像 E / 投薬 F / 注射 G / リハ H / 精神 I / 処置 J / 手術 K / 麻酔 L /
  放射線治療 M / 病理 N / その他 O)。**まず部でディレクトリを切り、区分が多い部だけ
  告示の 節 → 款 → 群 の順にさらに深くしてある** (深さは部によって違う。手術 K は
  `K_手術/第01節_手術料/第09款_腹部/群03_胃、十二指腸/` まで 4 段)
- `医科点数表R08_介護老人保健施設入所者` … 第3章
- `医療観察点数表R08` / `入院時食事療養R08` / `点数表外コードR08` …
  **医科点数表 (告示別表第一) ではない**。医科診療行為マスターに相乗りしている
  別告示・レセ電内部コードの分で、**告示・通知の本文は収録していない** (コード表だけ)
- `施設基準R08_基本診療料` … 「基本診療料の施設基準等及びその届出に関する手続きの
  取扱いについて」(令和8年3月5日 保医発0305第7号、0730訂正後) の本文と別添1〜5。
  第1章の「別に厚生労働大臣が定める施設基準」の**中身**。1 ノード = 通知の「第N」= 1 施設基準。
  別添ごとにディレクトリ
- `施設基準R08_特掲診療料` … 同「特掲診療料の…」(保医発0305第8号、0730訂正後)。
  第2章分。点数表の部でディレクトリ (手術 K は款まで)。歯科・調剤の施設基準も通知に
  ある分は入っている (`歯科M_…` / `歯科N_…` / `調剤`)
- `疑義解釈R08` … 令和8年度改定の疑義解釈資料 (事務連絡) の Q&A、**医科の別添**
  (別添1 医科 + 看護職員処遇改善評価料・ベースアップ評価料の別添の医科に関わる問)。
  **その1〜その12 (2026-09-02 発出) まで**。継続発出中なので、それ以降の その N は
  改定特設ページで確認する。1 ノード = 1 区分番号の Q&A 束 (点数表側と同じ basename)、
  区分番号が読めない問は `区分番号なし/` に原本の見出しで束ねてある
- `DPC点数表R08` … **診断群分類 (DPC) 電子点数表** (令和8年度改定、正式版 2026-08-12 更新分)。
  急性期入院の包括払い (DPC/PDPS) で使う **14 桁の診断群分類番号と入院期間Ⅰ/Ⅱ/Ⅲ ごとの
  1 日あたり点数**。1 ノード = 1 分類 (6 桁、504 本)、MDC (2 桁) でディレクトリ。分類ノードに
  対象傷病 (ICD)・分岐の定義 (K コード / 処置等 / 副傷病 / 重症度)・全 14 桁の点数表が入る。
  補助に `00_DPCの読み方` (共通ルール) / `00_北大病院の係数` (医療機関別係数) / 留意事項通知・
  参加手続き通知の全文。**医科点数表 (出来高) とは別の点数表** — 入院の包括点数はこちら、
  出来高部分と DPC 対象外の患者は医科点数表

**当院の届出状況** (施設基準登録者一覧・令和8年7月時点) は院内イントラ由来なので
`{dir_ref("/診療報酬")}` ではなく `{dir_ref(TOUROKU_DOC)}` にある (持ち出し禁止のカテゴリ)。
施設基準ノードと登録者一覧ノードは互いに path を名乗る。
「当院で算定できるか」は 点数表 → 施設基準 → 登録状況 の 3 つを read してから答える。

## ライセンス・出典

厚生労働省ホームページのコンテンツで、**mhlw.go.jp 全体の利用規約**
(<https://www.mhlw.go.jp/chosakuken/>) が適用される。中身は
**公共データ利用規約 (第1.0版) / PDL1.0** 準拠:

- 複製・公衆送信・翻訳・変形等の二次利用は**商用・非商用を問わず自由**
- 条件は (1) **出典の記載**、(2) 編集・加工した場合はその旨の明記、
  (3) 国が作成したかのような態様での公表の禁止

本ナレッジベースは各ノードに出典 URL を持たせ、告示・通知の本文を機械抽出した
**加工物**である旨をここに明記することで条件を満たしている。

出典:

- 令和8年度診療報酬改定について (厚生労働省) <{SOURCE}>
- 診療報酬情報提供サービス (厚生労働省保険局) <{MASTER_SOURCE}>
"""


def unassigned_block(rows: list[list[str]], where: str) -> list[str]:
    if not rows:
        return []
    return [
        f"## 区分番号が振られていない診療行為コード ({len(rows)} 件)",
        "",
        "医科診療行為マスターで**コード表用番号の区分番号が `000`・枝番が `00`・"
        "項番が `000`** = 点数表の区分に紐づいていない行。"
        f"{where}"
        "**区分番号がまさに `000` の区分 (A000 初診料 / C000 往診料 など) の"
        "ノードに混ぜると別物の加算に見えてしまう**ので、索引側に出してある。",
        "",
    ] + code_table(rows, master_hint=False)


def master_note() -> str:
    return (
        f"**コードを網羅的に絞り込む・数値条件で引くなら、doc の表ではなく"
        f"マスター CSV の原本を使う**: `{MASTER_CSV_URL}` ({MASTER_CSV_DESC})。"
        f"sandbox に取り込んで pandas で引ける。doc 側は算定要件の自然文が本体で、"
        f"CSV 側がコードの網羅と数値条件、と役割が分かれている。"
    )


def genban_block(manifest: dict, *, master_only: bool = False) -> list[str]:
    lines = ["## 原本の版", ""]
    if not master_only:
        lines += [
            f"- 告示 PDF: {manifest.get('kokuji', '(manifest なし)')}",
            f"- 通知 PDF: {manifest.get('tsuchi', '(manifest なし)')}",
        ]
    lines += [f"- マスター: {manifest.get('master', '(manifest なし)')}", ""]
    if not master_only:
        lines += [
            "**告示・通知の PDF は訂正版に差し替えられ続ける** (特設ページに「0529訂正後」"
            "「0730訂正後」が併記される)。上の版表記より新しい訂正が出ていることがある。",
            "",
        ]
    return lines



# ---------------------------------------------------------------------------
# Section generation for chapters 1 / 2 (chapters where kokuji + tsuchi + master all exist)
# ---------------------------------------------------------------------------


def _orphan_title(key: str, in_chapter: dict) -> str:
    """Display name for a kubun key with no kokuji entry. **Built only from the names of its kokuji children**.

    Four kubun keys exist only in the master (B005-1 / C005-1 / D999 / E999).
    The first two exist in the kokuji only in three-level form (`Ｃ００５－１－２`), so
    listing the children's names reveals what they are. The last two are claim-system
    addresses with no fee-schedule provision (appendix 7-9 / 7-11), so **say so instead of inventing a name**.
    """
    kids = [in_chapter[c]["title"] for c in sorted(in_chapter) if c.startswith(key + "-")]
    if kids:
        return "・".join(kids) + "のコード表"
    return f"点数表に区分の規定がないコード ({key})"


def _resolve_ctx(key: str, kok: dict | None, in_chapter: dict, rows: list[list[str]]) -> dict:
    """Hierarchy (part/section/sub-section/group) of a kubun. Without a kokuji entry, borrow from a child or the CSV part."""
    if kok:
        return dict(kok["ctx"])
    child = next((c for c in sorted(in_chapter) if c.startswith(key + "-")), None)
    if child:
        return dict(in_chapter[child]["ctx"])
    if rows:
        part = int(rows[0][C_PART])
        ref = next(
            (v for v in in_chapter.values() if v["ctx"].get("部", (0, ""))[0] == part),
            None,
        )
        if ref:
            return {"章": ref["ctx"]["章"], "部": ref["ctx"]["部"]}
    return {}


def part_dirname(ctx_part: tuple[int, str], alpha: str) -> str:
    """Name of a part directory. **The alphabet part leads** (`K_手術`).

    Staff say "K codes" / "D codes", so this is easier to find than the part number.
    A–O also sorts so that `/doc/list` name order matches the kokuji part order.
    """
    num, name = ctx_part
    return f"{alpha}_{fs_safe(name)}" if alpha else f"第{num:02d}部_{fs_safe(name)}"


def plan_tree(
    keys: list[str], nodes: dict, depth: int, rel: str, doc: str, ctx: dict, out: list
) -> None:
    """Fix the hierarchy up front (settle "who lives where" before writing leaves).

    Pushes (rel, doc, ctx, direct kubun, [(child dir name, child kubun count)]) onto `out`.
    **Two passes because three-level kubun nodes must name the parent's path as
    "codes live in the parent node"**. Writing as we go would leave the parent path undecided.
    """
    direct, subs = collapse(keys, nodes, depth)
    children = []
    for name, (path, ks) in subs.items():
        cctx = dict(ctx)
        for lv, num, nm in path:
            cctx[lv] = (num, nm)
        plan_tree(ks, nodes, depth + 1, f"{rel}/{name}", f"{doc}/{name}", cctx, out)
        children.append((name, len(ks)))
    for key in direct:
        nodes[key]["doc_dir"] = doc
        nodes[key]["rel_dir"] = rel
    # `keys` is **every kubun under this level** (direct + contents of child dirs).
    # Used for the kubun-number range in the front-matter kubun
    out.append((rel, doc, ctx, direct, children, keys))


def build_kubun_chapter(
    *,
    chapter: str,
    section_name: str,
    chapter_label: str,
    intro: list[str],
    kokuji: dict,
    tsuchi: dict,
    tsusoku: dict[tuple, dict],
    csv_rows: list[list[str]],
    manifest: dict,
    flat: bool,
    # Other chapters lodged in this chapter's section index (chapter 4 transitional measures is
    # just 2 entries in the kokuji, too small for its own section, and is about chapter-1 inpatient fees)
    extra_chapters: tuple[str, ...] = (),
    # Whether to place the all-chapter preamble from the head of the tsuchi (**true in exactly one place**)
    include_global: bool = False,
) -> dict:
    sec = Section(section_name)
    master, unassigned = load_master(csv_rows, chapter, kokuji=kokuji)

    # Node set = master kubun keys ∪ kokuji kubun anchors (minus 削除).
    # **Unlike chapter 1, chapter 2 leaks in both directions**:
    #   - in kokuji but not master (46) … drug fees / specific medical materials / film /
    #     oxygen add-on (priced by drug / material price lists, so absent from the procedure master) and
    #     three-level kubun numbers (rounded into the branch)
    #   - in master but not kokuji (4) … B005-1 / C005-1 / D999 / E999
    in_chapter = {
        k: v for k, v in kokuji.items() if v["ctx"].get("章", (0, ""))[0] == int(chapter)
    }
    keys = sorted(set(master) | {k for k, v in in_chapter.items() if v["title"] != "削除"})

    # Three-level kubun numbers → the master lumps them at the second level. Record where they live
    lumped = {
        k: k.rsplit("-", 1)[0]
        for k in keys
        if k.count("-") == 2 and k not in master and k.rsplit("-", 1)[0] in master
    }
    covers: dict[str, list[str]] = defaultdict(list)
    for k, p in lumped.items():
        covers[p].append(k)

    nodes: dict[str, dict] = {}
    for key in keys:
        kok = in_chapter.get(key)
        rows = master.get(key, [])
        ctx = _resolve_ctx(key, kok, in_chapter, rows)
        nodes[key] = {
            "kokuji": kok,
            "tsuchi": tsuchi.get(key),
            "rows": rows,
            "title": kok["title"] if kok else _orphan_title(key, in_chapter),
            "ctx": ctx,
            "ctx_path": _ctx_path(ctx),
            "section": ctx_label(ctx),
            "order": kok["line_no"] if kok else 10**9,
            "lumped_key": lumped.get(key),
            "covers": sorted(covers.get(key, [])),
        }
    for key, node in nodes.items():  # kubun absent from the kokuji go right before their child
        if node["order"] == 10**9:
            child = next((c for c in sorted(nodes) if c.startswith(key + "-")), None)
            node["order"] = nodes[child]["order"] - 1 if child else 10**9

    order = lambda k: (nodes[k]["order"], k)

    # --- Fix the hierarchy first -----------------------------------------------------
    plan: list = []
    if flat:
        for key in nodes:
            nodes[key]["doc_dir"] = sec.doc
            nodes[key]["rel_dir"] = ""
        top_children: list[tuple[str, int]] = []
    else:
        parts: dict[tuple, list[str]] = defaultdict(list)
        for k, n in nodes.items():
            if not n["ctx_path"]:
                sys.exit(f"{k}: part undetermined (cannot be placed in the hierarchy)")
            parts[n["ctx_path"][:1]].append(k)
        top_children = []
        for p in sorted(parts, key=lambda p: p[0][1]):
            lv, num, name = p[0]
            alpha = next((k[0] for k in sorted(parts[p]) if k[1:4].isdigit()), "")
            dname = part_dirname((num, name), alpha)
            plan_tree(
                sorted(parts[p], key=order),
                nodes,
                1,
                dname,
                f"{sec.doc}/{dname}",
                {"章": (int(chapter), chapter_label), lv: (num, name)},
                plan,
            )
            top_children.append((dname, len(parts[p])))

    for key, node in nodes.items():
        if node["lumped_key"]:
            parent = nodes[node["lumped_key"]]
            at = f"{parent['doc_dir']}/{slug(node['lumped_key'], parent['title'])}"
            # Whether the parent is split into child nodes (= a directory) is decided by leaf_md,
            # so dry-run the same decision as the write to match the shape (`_index.md` + children if split)
            _name, parent_files = leaf_md(node["lumped_key"], parent, "", chapter_label)
            node["lumped_at"] = dir_ref(at) if len(parent_files) > 1 else leaf_ref(at)

    # --- Assign the provisions ------------------------------------------------
    # **A level with its own directory first, else the nearest ancestor, else the section index.**
    # Chapters have no directory, so provisions tied only to a chapter always land in the section index
    ts_dirs = {_ts_key(c): r for r, _d, c, _di, _ch, _ak in plan}
    ts_here, ts_section = assign_tsusoku(
        tsusoku,
        ts_dirs,
        chapters={chapter, *extra_chapters},
        include_global=include_global,
    )

    # --- Write out -------------------------------------------------------------
    if sec.dir.exists():
        shutil.rmtree(sec.dir)
    sec.dir.mkdir(parents=True)

    sizes: dict[str, int] = {}
    split_keys: list[str] = []

    def write_leaves(keys_here: list[str], rel: str, doc: str) -> None:
        for key in sorted(keys_here, key=order):
            name, files = leaf_md(key, nodes[key], doc, chapter_label)
            for r, text in files.items():
                out = sec.dir / rel / r if rel else sec.dir / r
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(text, encoding="utf-8")
                stem = r.rsplit("/", 1)[-1][:-3]
                node_id = key if stem in ("_index", name) else f"{key}/{stem}"
                sizes[node_id] = len(text.split("---\n", 2)[-1])
            if len(files) > 1:
                split_keys.append(key)

    if flat:
        write_leaves(list(nodes), "", sec.doc)
        top_un = unassigned
    else:
        for rel, doc, ctx, direct, children, all_keys in plan:
            (sec.dir / rel).mkdir(parents=True, exist_ok=True)
            write_leaves(direct, rel, doc)
            (sec.dir / rel / "_index.md").write_text(
                group_index(
                    ctx, doc, direct, children, all_keys, nodes, ts_here[rel], unassigned
                ),
                encoding="utf-8",
            )
            sizes[doc] = 0
        top_un = []

    (sec.dir / "_index.md").write_text(
        section_index(
            sec,
            chapter_label,
            intro,
            nodes,
            split_keys,
            top_children,
            top_un,
            manifest,
            ts_section,
            flat=flat,
        ),
        encoding="utf-8",
    )
    return {
        "kubun": len(nodes),
        "nodes": len(sizes),
        "split": sorted(split_keys),
        "codes": sum(len(n["rows"]) for n in nodes.values()),
        "unassigned": len(unassigned),
        "sizes": {k: v for k, v in sizes.items() if v},
        "no_tsuchi": sorted(k for k, n in nodes.items() if n["tsuchi"] is None),
        "no_kokuji": sorted(k for k, n in nodes.items() if n["kokuji"] is None),
        "no_rows": sorted(k for k, n in nodes.items() if not n["rows"]),
        "lumped": sorted(lumped),
        # Where the provisions ended up. **Always surface the dropped count** (reporting only
        # the parsed count hides the ones discarded for lack of a home)
        "ts_dir": sum(len(v) for v in ts_here.values()),
        "ts_section": len(ts_section),
        "dirs": len(plan),
        "max_children": max(
            [len(d) + len(c) for _r, _d, _c2, d, c, _a in plan] + [len(nodes) if flat else 0]
        ),
    }


TS_SRC_LABEL = {"kokuji": "告示", "tsuchi": "留意事項通知"}
# Home of the all-chapter preamble at the head of the tsuchi. **Placed once; other chapters refer to it**
TS_GLOBAL_AT = dir_ref(f"/診療報酬/{SEC_CH1}")
TS_GLOBAL_NOTE = (
    f"**留意事項通知の冒頭にある総則 (全章共通、「医科診療報酬点数表に関する事項」1〜9) は "
    f"`{TS_GLOBAL_AT}` の本文にある** — 費用の総計の出し方・基本診療料に含まれるもの・"
    "施設基準の根拠告示など、この章にも効く。"
)


def tsusoku_block(src: str, blk: dict, here_ctx: dict) -> list[str]:
    """Heading + verbatim text of one provisions block.

    The first choice of home is "the directory of that level", but **parts whose sections /
    sub-sections are not directories** (imaging, injections, pathology …) and **chapters without
    a directory** (chapters 1, 3, 4) exist, so those go onto the **nearest ancestor node**. The
    heading states which level the provisions belong to (`## 留意事項通知の通則 — 第1節 エックス線診断料`).
    """
    label2 = TS_SRC_LABEL[src]
    ctx = blk["ctx"]
    ch = ctx.get("章")
    below = ctx_label_below(ctx, here_ctx)
    if not ch:
        # The preamble 「医科診療報酬点数表に関する事項」 at the head of the tsuchi. Sits above the chapters
        head = f"## {label2}の総則 (全章共通)"
        note = (
            f"{label2}の冒頭にある**総則**。**第1章から第3章までの全章に効く**"
            "前置きで、区分のノードにも章のセクションにも入っていない。"
        )
    elif not below and ctx_label(ctx):
        # Provisions of this level's own directory (the existing form)
        head = f"## {label2}の通則"
        note = (
            f"{ctx_label(ctx)} の**通則**。個々の区分の点数・注より前に効く条文で、"
            "区分のノードには入っていない。"
        )
    elif ch[0] == 4:
        # Chapter 4 transitional measures is the chapter itself, not "provisions" (just 2 entries in the kokuji)
        head = f"## {label2} 第4章 {ch[1]}"
        note = (
            f"医科点数表の**第4章 {ch[1]}**。章が 2 項しかないのでセクションを立てず、"
            "中身が第1章 (入院基本料) の経過措置なのでここに置いてある。"
        )
    else:
        chap = f"第{ch[0]}章 {ch[1]}"
        where = below or chap
        head = f"## {label2}の通則 — {where}"
        full = f"{chap} {ctx_label(ctx)}".strip()
        note = (
            f"{full} の**通則**。個々の区分の点数・注より前に効く条文で、"
            "区分のノードには入っていない。"
            + (
                "**この階層はディレクトリになっていない**ので、直近の親ノードである"
                "ここに置いてある。"
                if below
                else ""
            )
        )
    return [head, "", note, "", "```", blk["body"], "```", ""]


def assign_tsusoku(
    tsusoku: dict[tuple, dict],
    ts_dirs: dict[tuple, str],
    *,
    chapters: set[str],
    include_global: bool,
) -> tuple[dict[str, list], list]:
    """Distribute provisions between "hierarchy directories" and "the section index".

    **Try ancestors from the tail of the key**. If no directory exists anywhere, the section index.
    Returns ({rel: [(src, blk)]}, [(src, blk)]).
    """
    here: dict[str, list] = defaultdict(list)
    section: list = []
    # Order **kokuji → tsuchi** (do not interleave the two sources). Within one source, source order
    order = lambda kv: (0 if kv[0][0] == "kokuji" else 1, kv[1]["line_no"])
    for (src, key), blk in sorted(tsusoku.items(), key=order):
        ch = blk["ctx"].get("章")
        if ch is None:
            if not include_global:
                continue
        elif str(ch[0]) not in chapters:
            continue
        for i in range(len(key), 0, -1):
            rel = ts_dirs.get(key[:i])
            if rel is not None:
                here[rel].append((src, blk))
                break
        else:
            section.append((src, blk))
    return here, section


def unassigned_for(ctx: dict, unassigned: list[list[str]]) -> list[list[str]]:
    """"No kubun" rows belonging to that part. Emitted **only in the part directory's index**."""
    if "部" not in ctx or any(lv in ctx for lv in ("節", "款", "群")):
        return []
    part = ctx["部"][0]
    return [r for r in unassigned if int(r[C_PART]) == part]


def group_index(
    ctx: dict,
    doc: str,
    direct: list[str],
    child_dirs: list[tuple[str, int]],
    all_keys: list[str],
    nodes: dict,
    ts_items: list[tuple[str, dict]],
    unassigned: list[list[str]],
) -> str:
    """Directory node for a part / section / sub-section / group. **Kokuji / tsuchi provisions go here**.

    Provisions are "clauses that apply before the individual kubun" (surgery after-hours add-on,
    test interpretation fees …); they belong to no kubun anchor, so they are not in kubun nodes.
    The chapter-1 implementation discarded them, but in chapter 2 billing cannot be assembled
    without them. The hierarchy node is their proper home.

    **Provisions of levels that are not directories also land here** (`assign_tsusoku` pushes
    them to the nearest ancestor). The heading states which level they belong to.
    """
    label = ctx_label(ctx)
    lv = [x for x in _LEVELS if x in ctx][-1]
    keys = sorted(direct, key=lambda k: (nodes[k]["order"], k))
    span = sorted(all_keys, key=lambda k: (nodes[k]["order"], k))
    kubun_range = (
        (span[0] if len(span) == 1 else f"{span[0]}〜{span[-1]}") if span else ctx[lv][1]
    )
    body = [
        f"{TENSUHYO} {ctx.get('章', (0, ''))[1]} {label}。",
        "",
        scope_note(),
        "",
    ]
    if child_dirs:
        body += [
            f"**この階層は {len(child_dirs)} 個の下位ディレクトリに分かれている** "
            f"(1 回の list が返す子を {CHILD_LIMIT} 個程度に抑えるため、"
            f"告示にある区切り (節・款・群) で割ってある)。",
            "",
        ] + [f"- `{dir_ref(f'{doc}/{n}')}` — {c} 区分" for n, c in child_dirs] + [""]
    if direct:
        body += [f"**この階層の直下に {len(direct)} 区分ある** (`{keys[0]}` 〜 `{keys[-1]}`)。", ""]
    for src, blk in ts_items:
        body += tsusoku_block(src, blk, ctx)
    body += unassigned_block(
        unassigned_for(ctx, unassigned),
        f"{label} では次の行がこれに当たる。",
    )
    # Part nodes **lead with the alphabet part, like `K 手術`** (staff say "K codes",
    # so it is easy to spot in the list output)
    name = ctx[lv][1]
    if lv == "部":
        alpha = next((k[0] for k in span if k[1:4].isdigit()), "")
        if alpha:
            name = f"{alpha} {name}"
    return "\n".join(
        front_matter(name, kubun_range, label, child_fields=True) + body
    )


def section_index(
    sec: Section,
    chapter_label: str,
    intro: list[str],
    nodes: dict,
    split_keys: list[str],
    top_children: list[tuple[str, int]],
    unassigned: list[list[str]],
    manifest: dict,
    ts_section: list[tuple[str, dict]],
    *,
    flat: bool,
) -> str:
    lines = [
        "---",
        f"title: {yaml_str('医科点数表 ' + chapter_label + ' (令和8年度改定)')}",
        f"tensuhyo: {yaml_str(TENSUHYO)}",
        f"publisher: {PUBLISHER}",
        # **Quote it**. Bare, YAML turns it into datetime.date in meta, and since it has
        # no `?` = a field shown by /doc/list, that drags JSON-serialisation trouble in
        f"applies_from: {yaml_str(APPLIES_FROM)}",
        f"source: {SOURCE}",
        f"retrieved_at: {yaml_str(RETRIEVED)}",
    ] + CHILD_FIELDS + ["---", ""] + intro + [
        "",
        "- **1 ノード = 1 区分番号**。`Ａ２３０` と `Ａ２３０－２` は別の区分なので別ノード "
        "(`A230` / `A230-2`)",
        "- 各区分は (1) 告示の点数と注、(2) 留意事項通知の算定要件、"
        "(3) 医科診療行為マスターの診療行為コードと点数、の 3 部構成 "
        "((3) が大きい区分だけ (3) を子ノードに出す — 下記)",
        f"- 適用は **{APPLIES_FROM}** から (令和8年度改定。薬価のみ 2026-04-01 先行)",
        "",
    ]
    if top_children:
        lines += [
            f"## 部 ({len(top_children)})",
            "",
            "**区分は部のディレクトリの下にある。** ディレクトリ名の頭文字が"
            "区分番号のアルファベット部 (いわゆる「Ｋコード」の K)。"
            f"区分が多い部は告示の節・款・群でもう 1 段深い "
            f"(1 回の list が返す子を {CHILD_LIMIT} 個程度に抑えるため)。"
            "**通則 (部・節・款・群ごとの前置き条文) は各ディレクトリの本文に入れてある** — "
            "ディレクトリになっていない階層 (節・款を切っていない部の節通則など) の分は、"
            "**直近の親ディレクトリの本文に「〜の通則 — 第N節 …」の見出しで積んである**。",
            "",
        ] + [f"- `{dir_ref(f'{sec.doc}/{n}')}` — {c} 区分" for n, c in top_children] + [""]
    if ts_section:
        lead = (
            "章・部の通則のうち、置き場になるディレクトリが無いものをここに置いてある"
            f"({len(ts_section)} ブロック)。**個々の区分の点数・注より前に効く**ので、"
            "区分ノードだけを読んで算定を組み立てないこと。"
        )
        if not any(b["ctx"].get("章") is None for _s, b in ts_section):
            lead += TS_GLOBAL_NOTE
        lines += ["## このセクション全体に効く前置き条文", "", lead, ""]
        for src, blk in ts_section:
            lines += tsusoku_block(src, blk, {})
    lines += [
        f"**コード表が {CODE_TABLE_SPLIT} 行を超える {len(split_keys)} 区分だけ、"
        f"(3) のコード表を子ノード `{CODE_CHILD}` に分けてある**。"
        f"区分本体 (告示 + 通知) がディレクトリノードになり、その下に "
        f"`{CODE_CHILD}` が入る。**区分本体を read してもコード表は返らない** — "
        f"本文の見出しがその旨を名乗るので、そこから子を read すること。",
        "",
        master_note(),
        "",
        "## 隣のセクション (このセクションには無いが、別のセクションに在る)",
        "",
        "- **施設基準の中身** (「別に厚生労働大臣が定める施設基準」= 施設基準通知の本文): "
        f"`{dir_ref(f'/診療報酬/{SEC_KIJUN_CH1}')}` (第1章分、保医発0305第7号) / "
        f"`{dir_ref(f'/診療報酬/{SEC_KIJUN_CH2}')}` (第2章分、保医発0305第8号)。"
        "**1 ノード = 通知の「第N」= 1 施設基準**で、各区分ノードの頭の「収録範囲の案内」が"
        "対応ノードの path を名乗る (名称で突き合わせており、結び付かなかった区分は名称で grep)",
        f"- **疑義解釈** (Q&A): `{dir_ref(f'/診療報酬/{SEC_QA}')}`。**{QA_AS_OF}**を収録。"
        "区分番号ごとに 1 ノードで、点数表側と同じ basename。"
        f"疑義解釈は継続発出中で本体に統合されないため、それ以降の その N は未収録 — "
        f"{QA_LATEST_HINT} で確認すること",
        f"- **当院の届出状況**: `{dir_ref(TOUROKU_DOC)}` (施設基準登録者一覧・{TOUROKU_AS_OF}、院内イントラ由来)。"
        "**施設基準ごとに専任・専従で登録している職員の名簿**であって、届出済み施設基準の一覧ではない "
        "(職員配置を要しない施設基準は載らない)。「当院で算定できるか」は "
        "点数表 → 施設基準 → 登録状況 の 3 つを read してから答え、"
        f"最終的な届出の有無は {TOUROKU_CONTACT} に確認させること",
        f"- **DPC (診断群分類) 電子点数表**: `{dir_ref(f'/診療報酬/{SEC_DPC}')}`。急性期入院の包括払いの 14 桁コードと"
        "入院期間Ⅰ/Ⅱ/Ⅲ の 1 日あたり点数 (1 ノード = 1 分類、MDC でディレクトリ)。**当院は DPC 対象病院**なので、"
        "一般病棟の包括払い患者の入院料はこの点数表の入院基本料 × 日数ではない。入院料等のリーフが 1 行で送る",
        "",
        "## 収録していないもの (ここに無い = 原本を見る)",
        "",
        "- 薬価・特定保険医療材料・歯科・調剤の各点数表 (疑義解釈もこれらの別添は未収録)",
        "- 施設基準の**告示** (基本診療料の施設基準等 / 特掲診療料の施設基準等) と届出の様式。"
        "通知の別添が同じ基準を横書きで持っているので通知だけ収録している",
        "- 留意事項通知の末尾にある **別表1「第1節入院基本料、第3節特定入院料及び"
        "第4節短期滞在手術等基本料との関係」**(入院基本料等加算 × 入院料の算定可否表)。"
        "原本 PDF が横に 200 列以上ある表で、テキストに落とすと列が縦に割れて"
        "**加算と可否の対応が失われる**ため収録していない。加算が特定入院料に"
        "包括されるかは、この表の原本 (通知 PDF の末尾) を見ること",
        "- 同じく末尾の **別添1の2 (別紙様式)** と各様式。通知本文が「書面等は"
        "別紙様式のとおり」と送っているだけで、様式そのものは収録していない",
        "",
    ] + genban_block(manifest)
    lines += unassigned_block(
        unassigned, f"医科点数表 {chapter_label} では次の {len(unassigned)} 件がこれに当たる。"
    )
    if flat:
        parts: dict[str, list[str]] = defaultdict(list)
        for key, n in sorted(nodes.items()):
            mark = ""
            if len(n["rows"]) > CODE_TABLE_SPLIT:
                mark = f" — コード表 {len(n['rows'])} 件は子ノード `{CODE_CHILD}`"
            parts[n["section"]].append(f"- `{key}` {n['title']}{mark}")
        lines += ["## 区分一覧", ""]
        for head in sorted(parts):
            lines += [f"### {head}", ""] + sorted(parts[head]) + [""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Chapter 3: fees for residents of geriatric health facilities (介護老人保健施設入所者)
# ---------------------------------------------------------------------------
#
# **A chapter without kubun numbers**. Both kokuji and tsuchi only raise entries with Arabic
# numerals like `１ 緊急時施設治療管理料` under 「第1部 …」, with no alphabet part like Ａ／Ｂ.
# The master's alphabet part is `－` too, and kubun 001 exists in both part 1 and part 2
# (**without the part in the key, two kubun collapse into one**).
# The whole chapter is small (50 kokuji lines, 70 tsuchi lines, 2 codes), so **one node per part**.

CH3_PART_RE = re.compile(r"^[\s\x0c]*([０-９0-9]{1,2})[\s　]+(\S.*)$")


def build_chapter3(
    kokuji_lines: list[str],
    tsuchi_lines: list[str],
    csv_rows: list[list[str]],
    manifest: dict,
    tsusoku: dict[tuple, dict],
) -> dict:
    sec = Section(SEC_CH3)
    master, unassigned = load_master(csv_rows, "3")
    if unassigned:
        sys.exit("chapter 3 produced rows with no kubun (unexpected; revisit load_master)")
    if sec.dir.exists():
        shutil.rmtree(sec.dir)
    sec.dir.mkdir(parents=True)

    parts = _split_chapter(kokuji_lines, "第3章"), _split_chapter(tsuchi_lines, "第3章")
    sizes = {}
    listed = []
    for num in sorted(set(parts[0]) | set(parts[1])):
        kok = parts[0].get(num, {})
        tsu = parts[1].get(num, {})
        name = kok.get("name") or tsu.get("name") or f"第{num}部"
        key = f"P{num}"
        rows = [r for r in csv_rows if r[C_CHAPTER] == "3" and int(r[C_PART]) == num]
        label = f"第{num}部 {name}"
        fname = f"第{num}部_{fs_safe(name)}"
        body = [
            f"{TENSUHYO} 第3章 介護老人保健施設入所者に係る診療料 {label}。"
            f"適用は {APPLIES_FROM} から。",
            "",
            "**第3章には区分番号 (Ａ／Ｂ… のアルファベット部) が無い。** 告示も通知も"
            "部の下に `１ 緊急時施設治療管理料` のように算用数字で項を立てるだけなので、"
            "**部を 1 ノード**にして部の全文を収めてある "
            "(第1章・第2章の「1 ノード = 1 区分番号」とは粒度が違う)。",
            "",
            "施設入所者に対しては第1章・第2章は適用されず、この章の定めによる "
            "(ただし「その他の診療料」は第1章・第2章の例による)。",
            "",
            scope_note(),
            "",
            "## 告示 (点数と注)",
            "",
            "「診療報酬の算定方法」(平成20年厚生労働省告示第59号) 別表第一 "
            "医科診療報酬点数表 第3章。",
            "",
            "```",
            kok.get("body", "(告示に該当部分が無い)"),
            "```",
            "",
            "## 留意事項通知 (算定要件)",
            "",
            "「診療報酬の算定方法の一部改正に伴う実施上の留意事項について」"
            "(令和8年3月5日 保医発0305第6号) 別添1 第3章。",
            "",
            "```",
            tsu.get("body", "(通知に該当部分が無い)"),
            "```",
            "",
            f"## 診療行為コードと点数 ({len(rows)} 件)",
            "",
            "医科診療行為マスター (コード表用番号 章=3) の当該部の全行。"
            "**マスターのアルファベット部は `－`** で、区分番号は部ごとに 001 から"
            "振り直される (部を見ないと第1部と第2部が衝突する)。",
            "",
        ]
        text = "\n".join(
            front_matter(name, key, label, child_fields=False)
            + body
            + code_table(rows)
        )
        (sec.dir / f"{fname}.md").write_text(text, encoding="utf-8")
        sizes[key] = len(text.split("---\n", 2)[-1])
        listed.append(f"- `{leaf_ref(f'{sec.doc}/{fname}')}` — {label} ({len(rows)} コード)")

    intro = [
        "医科診療報酬点数表の **第3章 介護老人保健施設入所者に係る診療料**。"
        "介護老人保健施設の入所者に対する診療は、第1章・第2章ではなくこの章による。",
        "",
        "**この章には区分番号が無い**ので、**1 ノード = 1 部** (第1章・第2章の"
        "「1 ノード = 1 区分番号」とは粒度が違う)。",
        "",
    ] + listed + [""]
    # Preamble at the head of the chapter (both kokuji and tsuchi: "chapters 1–2 do not apply to residents").
    # **Not under a part, so not in a part node** — the section index is its home.
    # Part provisions are not emitted here since the part node holds the full text
    ts_section = [
        (src, blk)
        for (src, key), blk in sorted(
            tsusoku.items(), key=lambda kv: (0 if kv[0][0] == "kokuji" else 1, kv[1]["line_no"])
        )
        if len(key) == 1 and key[0] == ("章", 3)
    ]
    if ts_section:
        intro += [
            "**章の頭にある前置き条文は下の「〜の通則」に入れてある** "
            "(部のノードには入っていない)。" + TS_GLOBAL_NOTE,
            "",
        ]
        for src, blk in ts_section:
            intro += tsusoku_block(src, blk, {})
    (sec.dir / "_index.md").write_text(
        "\n".join(
            [
                "---",
                "title: '医科点数表 第3章 介護老人保健施設入所者に係る診療料 (令和8年度改定)'",
                f"tensuhyo: {yaml_str(TENSUHYO)}",
                f"publisher: {PUBLISHER}",
                f"applies_from: {yaml_str(APPLIES_FROM)}",
                f"source: {SOURCE}",
                f"retrieved_at: {yaml_str(RETRIEVED)}",
            ]
            + CHILD_FIELDS
            + ["---", ""]
            + intro
            + [master_note(), ""]
            + genban_block(manifest)
        ),
        encoding="utf-8",
    )
    return {
        "nodes": len(sizes),
        "sizes": sizes,
        "codes": sum(len(master[k]) for k in master),
        "ts_section": len(ts_section),
    }


def _split_chapter(lines: list[str], chapter: str) -> dict[int, dict]:
    """Cut the chapter-3 body by part (from `第N部 …` to the next part / next chapter)."""
    # **Do not trip over the TOC.** The first 100 lines of the kokuji are a table of contents that
    # also lists 「第3章 …」「第1部 …」. Restart from the last chapter heading seen (= the body).
    # The tsuchi has no chapter 4 after chapter 3; `別添１の２` and the appendix tables follow
    # directly, so **also close on line-head `別添` / `別表`** (otherwise the appendix is
    # swallowed whole and one node becomes 140K chars).
    out: dict[int, dict] = {}
    cur: int | None = None
    in_ch = False
    for line in lines:
        m = HEADING_RE.match(line)
        if m:
            lvl, num = m.group(2), int(m.group(1).translate(_Z2H))
            if lvl == "章":
                in_ch = f"第{num}章" == chapter
                if in_ch:
                    out, cur = {}, None
                continue
            if in_ch and lvl == "部":
                cur = num
                out[num] = {"name": re.sub(r"[\s　]+", " ", m.group(3).strip()), "lines": []}
                continue
        # **A form feed \x0c may lead the line** (`\x0c別添１の２`). No whitespace allowed —
        # an indented `別添６の別紙 14 …` is a reference within the body, not a divider
        if in_ch and re.match(r"^\x0c*別[添表]", line):
            in_ch = False
            continue
        if in_ch and cur is not None:
            out[cur]["lines"].append(line)
    for v in out.values():
        v["body"] = clean(v["lines"], drop_ruby=True)
    return out


# ---------------------------------------------------------------------------
# Chapters 6 / 8 / 9 — **not the medical fee schedule**. They ride along in the master
# ---------------------------------------------------------------------------
#
# `kokuji.pdf` / `tsuchi.pdf` contain only chapters 1–4 of the fee schedule (kokuji Appendix 1).
# Chapters 6, 8, 9 belong to other notifications / master specs; **their kokuji / tsuchi text is not fetched**.
# Only the code table is stored, and each node says "no clauses here".

MASTER_ONLY = {
    "6": dict(
        section=SEC_CH6,
        title="医療観察診療報酬点数表 (令和8年度改定・マスター収録分)",
        genten=(
            "心神喪失等の状態で重大な他害行為を行った者の医療及び観察等に関する法律"
            "第81条第1項に規定する指定医療機関の医療に要する費用の額の算定方法 "
            "(平成17年厚生労働省告示第365号。レセ電の仕様書では「医療観察診療報酬点数表」)"
        ),
        by_kubun=True,
    ),
    "8": dict(
        section=SEC_CH8,
        title="入院時食事療養・生活療養と標準負担額 (令和8年度改定・マスター収録分)",
        genten=(
            "「入院時食事療養費に係る食事療養及び入院時生活療養費に係る生活療養の費用の額の"
            "算定に関する基準」と「健康保険の食事療養標準負担額及び生活療養標準負担額」"
            "(平成8年厚生省告示第203号)"
        ),
        by_kubun=False,
        short="入院時食事療養・生活療養と標準負担額",
    ),
    "9": dict(
        section=SEC_CH9,
        title="医科点数表に規定のない診療行為コード (令和8年度改定・マスター収録分)",
        genten=(
            "レセプト電算処理システムの内部コード。医科診療行為マスターの"
            "「コード表用番号 (アルファベット部)」が `＊` = 医科点数表に区分番号の設定が"
            "ない診療行為 (layout_rec1.pdf 別紙7-9「医科点数表等に規定のない診療行為コード」)"
        ),
        by_kubun=False,
        short="点数表に規定のない診療行為コード",
    ),
}

MASTER_ONLY_NOTE = (
    "> **このノードに告示・通知の条文は入っていない。** 収録しているのは"
    "医科診療行為マスター (診療行為コード・名称・点数) だけで、"
    "算定要件は原典の告示・通知を見ること。"
)


def build_master_only(chapter: str, csv_rows: list[list[str]], manifest: dict) -> dict:
    conf = MASTER_ONLY[chapter]
    sec = Section(conf["section"])
    master, unassigned = load_master(csv_rows, chapter)
    if unassigned:
        sys.exit(f"chapter {chapter} produced rows with no kubun (unexpected)")
    if sec.dir.exists():
        shutil.rmtree(sec.dir)
    sec.dir.mkdir(parents=True)

    sizes: dict[str, int] = {}
    listed: list[str] = []
    if conf["by_kubun"]:
        groups = master
    else:
        groups = {"ALL": [r for rs in master.values() for r in rs]}
    # Sort **part → kubun → branch** (not by procedure code).
    # File names are `P<part>-<kubun>[-<branch>]_…` too, so this matches /doc/list name order
    def _order(k: str) -> tuple:
        # Chapters without by_kubun have key "ALL", which is not numeric
        try:
            return (0,) + tuple(int(x) for x in k[1:].split("-"))
        except ValueError:
            return (1, k)

    for key in sorted(groups, key=_order):
        rows = groups[key]
        head = min(rows, key=lambda r: (r[C_SEQ], r[C_CODE]))
        name = row_name(head)
        if conf["by_kubun"]:
            label = f"第{int(head[C_PART])}部 区分番号{head[C_KUBUN]}" + (
                "" if head[C_BRANCH] == "00" else f"-{int(head[C_BRANCH])}"
            )
            fname = f"{key}_{fs_safe(name)}"
            title = name
        else:
            label = conf["title"]
            # **Never use the name `コード表`** — it collides with the child name used when a
            # kubun node splits a large block out, and verify would confuse the parent-child relation
            fname = fs_safe(conf["short"])
            title = conf["title"]
        body = [
            f"{conf['title']}。原典は{conf['genten']}。",
            "",
            MASTER_ONLY_NOTE,
            "",
            "**これは医科点数表 (「診療報酬の算定方法」別表第一) ではない。** "
            "医科診療行為マスターに相乗りしている別建ての点数表・コードで、"
            f"マスターの「コード表用番号 章」が `{chapter}` の行がこれに当たる。",
            "",
        ]
        if conf["by_kubun"]:
            body += [
                f"マスター上の位置: {label}。**区分の名称はマスターの項目名 "
                f"(項番の最も若い行の基本漢字名称) から採ったもので、告示の区分名そのもの"
                f"ではない** (告示を収録していないため確認できない)。",
                "",
            ]
        body += [f"## 診療行為コードと点数 ({len(rows)} 件)", ""]
        text = "\n".join(
            front_matter(
                title,
                key if conf["by_kubun"] else chapter,
                label,
                child_fields=False,
                # **Source is the master download page** — the fee-schedule PDFs on the
                # revision page contain none of this chapter's rows
                source=MASTER_SOURCE,
            )
            + body
            + code_table(rows)
        )
        (sec.dir / f"{fname}.md").write_text(text, encoding="utf-8")
        sizes[key] = len(text.split("---\n", 2)[-1])
        listed.append(f"- `{leaf_ref(f'{sec.doc}/{fname}')}` — {name} ({len(rows)} コード)")

    # **Keep the section index and the leaf from sharing a title.**
    # Chapters 8 / 9 have a single leaf whose title is conf['title'] itself; if the parent
    # had the same name, parent and child would look identical in the /doc/list output
    sec_title = (
        conf["title"] if conf["by_kubun"] else conf["short"] + " (セクションの案内)"
    )
    (sec.dir / "_index.md").write_text(
        "\n".join(
            [
                "---",
                f"title: {yaml_str(sec_title)}",
                f"tensuhyo: {yaml_str(conf['title'])}",
                f"publisher: {PUBLISHER}",
                f"applies_from: {yaml_str(APPLIES_FROM)}",
                f"source: {MASTER_SOURCE}",
                f"retrieved_at: {yaml_str(RETRIEVED)}",
            ]
            + CHILD_FIELDS
            + ["---", ""]
            + [
                f"{conf['title']}。原典は{conf['genten']}。",
                "",
                "**医科点数表 (「診療報酬の算定方法」別表第一) ではない。** "
                "医科診療行為マスターの「コード表用番号 章」が "
                f"`{chapter}` の行を集めたもので、"
                "改定特設ページの医科点数表 PDF (告示・通知) には 1 行も載っていない。"
                "**このセクションには告示・通知の条文が入っていない** — コードと点数だけ。"
                "算定要件は原典を見ること。",
                "",
                f"収録: {sum(len(v) for v in groups.values())} コード / {len(groups)} ノード。",
                "",
            ]
            + listed
            + ["", master_note(), ""]
            + genban_block(manifest, master_only=True)
        ),
        encoding="utf-8",
    )
    return {"nodes": len(sizes), "sizes": sizes, "codes": sum(len(v) for v in groups.values())}


# ---------------------------------------------------------------------------


def read_manifest() -> dict:
    mf = CACHE / "manifest.json"
    if not mf.exists():
        return {}
    src = json.loads(mf.read_text(encoding="utf-8")).get("sources", {})
    return {
        "kokuji": f"{src.get('kokuji.pdf', {}).get('note', '')} "
        f"{src.get('kokuji.pdf', {}).get('url', '')}".strip(),
        "tsuchi": f"{src.get('tsuchi.pdf', {}).get('note', '')} "
        f"{src.get('tsuchi.pdf', {}).get('url', '')}".strip(),
        "master": src.get("masters/s.csv", {}).get("note", ""),
    }


CH1_INTRO = [
    f"{TENSUHYO} の **第1章 基本診療料**。初診料・再診料・外来診療料と、",
    "入院料等 (入院基本料・入院基本料等加算・特定入院料・短期滞在手術等基本料) の全区分。",
    "",
    "**第2章 特掲診療料以降は別のセクションにある** — "
    f"`{dir_ref('/診療報酬')}` を list してセクションを選ぶこと (医学管理・在宅・検査・画像診断・投薬・注射・"
    "リハビリテーション・精神科専門療法・処置・手術・麻酔・放射線治療・病理診断は"
    "`医科点数表R08_特掲診療料`)。",
    "",
    "**このセクションだけ区分がフラットに並んでいる** (136 ノード)。"
    "第2章は 2 千区分あるので部でディレクトリを切ってあり、構造が非対称になっている。",
]

CH2_INTRO = [
    f"{TENSUHYO} の **第2章 特掲診療料**。医学管理等 (B) から その他 (O) までの全区分。",
    "",
    "**初診料・再診料・入院料等 (第1章 基本診療料) は別セクション** "
    f"`{dir_ref(f'/診療報酬/{SEC_CH1}')}` にある。",
]


def main() -> None:
    for name in ("kokuji.txt", "tsuchi.txt", "masters/s.csv"):
        if not (CACHE / name).exists():
            sys.exit(f"{CACHE / name} is missing. Run fetch_shinryo_hoshu.py first")

    kokuji, kok_ts, kok_shared = scan(CACHE / "kokuji.txt", drop_ruby=True)
    tsuchi, tsu_ts, tsu_shared = scan(CACHE / "tsuchi.txt", drop_ruby=False)
    tsusoku = {("kokuji", k): v for k, v in kok_ts.items()}
    tsusoku.update({("tsuchi", k): v for k, v in tsu_ts.items()})
    rows = master_rows()
    manifest = read_manifest()

    CATEGORY.mkdir(parents=True, exist_ok=True)
    (CATEGORY / "_index.md").write_text(CATEGORY_INDEX, encoding="utf-8")

    # Scan the neighbouring sections (Q&A / facility standards / registrant list) to resolve the real paths leaves name.
    # **Do not fail when absent** (on a first run the fee schedule must come first — the Q&A and
    # facility-standard builds read its node names). Without them only the section pointer is emitted, so
    # once the neighbours are built, build again to name the paths (`17_shinryo_hoshu.md`, "build order")
    XREF.load()
    qa_manifest = Path("cache/gigi_kaishaku/manifest.json")
    if qa_manifest.exists():
        # If the cutoff the Q&A side states and the QA_AS_OF baked here diverge, we lie that
        # "その13 does not exist". Hand-written constant, so check it against the manifest (written by fetch_gigi_kaishaku.py)
        latest = json.loads(qa_manifest.read_text(encoding="utf-8")).get("latest", {})
        want = f"その{latest.get('n')} ({latest.get('issued')} 発出) まで"
        if want != QA_AS_OF:
            sys.exit(
                f"QA_AS_OF={QA_AS_OF!r} does not match the Q&A manifest ({want!r}). "
                "Run build_gigi_kaishaku.py, then bump QA_AS_OF"
            )
    missing = [k for k, v in XREF.present.items() if not v]
    if missing:
        print(
            f"warning: neighbouring sections missing, kubun nodes carry only the section pointer: {missing}"
            " — build the Q&A / facility standards / DPC, then run this build again",
            file=sys.stderr,
        )

    stats = {}
    stats[SEC_CH1] = build_kubun_chapter(
        chapter="1",
        section_name=SEC_CH1,
        chapter_label="第1章 基本診療料",
        intro=CH1_INTRO,
        kokuji=kokuji,
        tsuchi=tsuchi,
        tsusoku=tsusoku,
        csv_rows=rows,
        manifest=manifest,
        flat=True,
        # **Chapter 4 transitional measures is just 2 entries in the kokuji**, about chapter-1
        # basic inpatient fees. Too small for its own section, so it lodges here
        extra_chapters=("4",),
        # The all-chapter preamble at the head of the tsuchi. **Placed in one spot only** (chapters 2–3 refer to it)
        include_global=True,
    )
    stats[SEC_CH2] = build_kubun_chapter(
        chapter="2",
        section_name=SEC_CH2,
        chapter_label="第2章 特掲診療料",
        intro=CH2_INTRO,
        kokuji=kokuji,
        tsuchi=tsuchi,
        tsusoku=tsusoku,
        csv_rows=rows,
        manifest=manifest,
        flat=False,
    )
    stats[SEC_CH3] = build_chapter3(
        (CACHE / "kokuji.txt").read_text(encoding="utf-8").split("\n"),
        (CACHE / "tsuchi.txt").read_text(encoding="utf-8").split("\n"),
        rows,
        manifest,
        tsusoku,
    )
    for ch in ("6", "8", "9"):
        stats[MASTER_ONLY[ch]["section"]] = build_master_only(ch, rows, manifest)

    all_sizes = {}
    for name, st in stats.items():
        for k, v in st.get("sizes", {}).items():
            all_sizes[f"{name}/{k}"] = v
    ordered = sorted(all_sizes.values())
    over9k = sorted((k for k in all_sizes if all_sizes[k] > 9000), key=lambda k: -all_sizes[k])
    big = max(all_sizes, key=lambda k: all_sizes[k])

    # **Reporting only the "parsed" provisions count hides the dropped ones.**
    # Always print the emitted count alongside those dropped for lack of a home
    ts_out = (
        stats[SEC_CH1]["ts_dir"]
        + stats[SEC_CH1]["ts_section"]
        + stats[SEC_CH2]["ts_dir"]
        + stats[SEC_CH2]["ts_section"]
        + stats[SEC_CH3]["ts_section"]
    )
    # Chapter-3 part provisions are held in full by the part node, so they do not count as unemitted
    ts_in_part = sum(
        1
        for (_src, key), blk in tsusoku.items()
        if len(key) > 1 and key[0] == ("章", 3)
    )
    ts_lost = len(tsusoku) - ts_out - ts_in_part
    ts_line = f"parsed{len(tsusoku)}/emitted{ts_out}/part_node{ts_in_part}/dropped{ts_lost}"
    if ts_lost:
        print(f"warning: {ts_lost} provisions block(s) had no home", file=sys.stderr)

    print(XREF.summary())
    for name in (SEC_CH1, SEC_CH2):
        st = stats[name]
        print(
            f"{name}: kubun={st['kubun']} nodes={st['nodes']} split={len(st['split'])} "
            f"codes={st['codes']} unassigned={st['unassigned']} "
            f"no_kokuji={len(st['no_kokuji'])} no_tsuchi={len(st['no_tsuchi'])} "
            f"no_rows={len(st['no_rows'])} lumped={len(st['lumped'])} "
            f"dirs={st['dirs']} max_children={st['max_children']} "
            f"tsusoku=dir{st['ts_dir']}/index{st['ts_section']}"
        )
    for name in (SEC_CH3, SEC_CH6, SEC_CH8, SEC_CH9):
        st = stats[name]
        print(f"{name}: nodes={st['nodes']} codes={st['codes']}")
    print(
        f"ALL: nodes={len(all_sizes)} "
        f"kokuji={len(kokuji)} tsuchi={len(tsuchi)} "
        f"shared_body=kokuji:{kok_shared}/tsuchi:{tsu_shared} "
        f"tsusoku={ts_line} ruby_dropped={RUBY_DROPPED} "
        f"max_body={all_sizes[big]}({big}) "
        f"median_body={ordered[len(ordered) // 2]} over9k={len(over9k)}"
    )
    print("  over9k: " + ", ".join(f"{k}:{all_sizes[k]}" for k in over9k[:20]))


if __name__ == "__main__":
    main()
