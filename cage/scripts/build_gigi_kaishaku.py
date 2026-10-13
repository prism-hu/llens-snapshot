#!/usr/bin/env python3
"""Convert the FY2026 (Reiwa 8) revision gigi kaishaku (Q&A notices; cache/gigi_kaishaku) into the doc tree `/診療報酬/疑義解釈R08`.

Input is `cache/gigi_kaishaku/sonoNN.txt` (pdftotext -layout) and `manifest.json` placed by
fetch_gigi_kaishaku.py. The design and the pitfalls of the source are documented in
`docs/cage/19_gigi_kaishaku.md` (single source of truth). Only implementation notes are kept here.

**Node = the bundle of Q&A for one kubun (category number of the medical fee schedule).** Questions
on the same kubun are scattered across issues (その N), so bundle by kubun rather than by issue.
One question = one node would exceed 500 nodes and make list unusable, and inserting a kubun
directory would mean digging 4 levels. Bundling by kubun gives a 1:1 correspondence with the fee
schedule nodes, so a model that read `/診療報酬/医科点数表R08_…/A000_初診料` can reach the
same-named node on the Q&A side in one step.

  - The kubun is the first kubun number of the form 「Ａ０００」 that appears in the **question
    body** (not the answer) and exists as a fee schedule node. Kubun that appear only in the answer
    are not used (answers often cite other kubun)
  - A question without a kubun goes under the kubun whose fee schedule node name matches the
    source 【見出し】 (heading); otherwise under `区分番号なし/` with **1 node = 1 heading**
  - Directories are one level by fee schedule part (`A_基本診療料` / `B_医学管理等` … `O_その他`).
    The fee schedule's hierarchy (section / subsection / group) is not reproduced here (kubun counts stay in 2 digits)

Structure of the source (one PDF):

    Cover (administrative notice. **Correction / abolition declarations are here**)
    （別添１） 医科診療報酬点数表関係          ← only this is taken (+ 医科・歯科診療報酬点数表関係)
        【見出し】                             ← may span several lines
        問N  …                                ← only a 「問」 at **line start (column 0)** is a question.
        （答）…                                   An indented 「問」 is a quotation (re-listed under （参考）)
    （別添２） 看護職員処遇改善評価料及びベースアップ評価料関係   ← not taken
    （別添３） 医科診療報酬点数表関係（ＤＰＣ）  ← not taken
    （別添N） 歯科 / 調剤 / 訪問看護 / 掲示事項   ← not taken
    （別添N） 医科診療報酬点数表関係            ← **a medical attachment other than 別添1 = a correction of an earlier その N**
                                                 (the cover declares 「別添１の問 26 について、別添６の
                                                 とおり訂正します」)

Corrections are **redline markup** (deletion = red strikethrough, insertion = red underline), and the
text layer keeps the deleted characters. Strikethrough cannot be read as text, so a human registers
them in `CORRECTION_STRIKES` / `TEISEI_CHECK` (an unregistered correction fails the build). The verify
side judges independently from the geometry with pdfplumber.

usage: uv run python scripts/build_gigi_kaishaku.py            (run from inside cage/)
       uv run python scripts/build_gigi_kaishaku.py --stats    (only print statistics, write no nodes)
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from textfix import DOC_BASE, doc_ref, fix_radicals, safe_segment  # noqa: E402

CACHE = Path("cache/gigi_kaishaku")
CATEGORY = Path("data/doc/診療報酬")
OUT = CATEGORY / "疑義解釈R08"
# Sections used to resolve kubun number → fee schedule node (the path target)
TENSUHYO_SECTIONS = (
    "医科点数表R08_基本診療料",
    "医科点数表R08_特掲診療料",
)
TENSUHYO = "医科診療報酬点数表 (令和8年度診療報酬改定)"
PUBLISHER = "厚生労働省"
APPLIES_FROM = "2026-06-01"
SOURCE = "https://www.mhlw.go.jp/stf/newpage_67729.html"

# References to other nodes in the body use the canonical form (leaf `{{DOC_BASE}}/….md` / directory `{{DOC_BASE}}/…/`).
# Format descriptions (containing `…` `<…>`, not matching a node) only get `{{DOC_BASE}}` prefixed
SECTION_REF = doc_ref("/診療報酬/疑義解釈R08", is_dir=True)
DPC_REF = doc_ref("/診療報酬/DPC点数表R08", is_dir=True)
DPC_TSUCHI_REF = doc_ref("/診療報酬/DPC点数表R08/00_DPC留意事項通知", is_dir=True)
SISETSU_KIHON_REF = doc_ref("/診療報酬/施設基準R08_基本診療料", is_dir=True)
SISETSU_TOKKEI_REF = doc_ref("/診療報酬/施設基準R08_特掲診療料", is_dir=True)
TENSU_KIHON_REF = doc_ref("/診療報酬/医科点数表R08_基本診療料", is_dir=True)
CATEGORY_REF = doc_ref("/診療報酬", is_dir=True)
# Our hospital's roster of dedicated / full-time staff (section on the in-house manual side)
SISETSU_TODOKEDE = doc_ref("/院内マニュアル/施設基準届出状況", is_dir=True)

# Headings of the attachments to take. 「医科・歯科」 holds common items that also apply to medical care, such as first / repeat visit fees and price-response fees.
# 「看護職員処遇改善評価料及びベースアップ評価料関係」 is an attachment mixing medical, dental, home nursing and pharmacy;
# questions on part O (O000〜O003) of the fee schedule exist only there, so take it (drop dental / pharmacy-only headings)
IKA_HEADING = "医科診療報酬点数表関係"
KANBE_HEADING = "看護職員処遇改善評価料及びベースアップ評価料関係"
# The DPC attachment is about medical inpatient care, so it counts as medical. The diagnosis group texts are in `{{DOC_BASE}}/診療報酬/DPC点数表R08/`
DPC_HEADING = "医科診療報酬点数表関係（ＤＰＣ）"
INCLUDE_HEADINGS = (IKA_HEADING, "医科・歯科診療報酬点数表関係", KANBE_HEADING, DPC_HEADING)
DPC_DIR = "DPC"
DPC_ROOT = CATEGORY / "DPC点数表R08"
# The DPC attachment (その2) is organised by numbered headings like `１．ＤＰＣ対象病院の基準について` instead of 【見出し】
DPC_CHAPTER_RE = re.compile(r"^\x0c?([０-９0-9]{1,2})．(\S.*)$")  # column 0 only (`10．` in the body is indented)
# Diagnosis group in the question body (top 6 digits, as in `胃の悪性腫瘍（060020）`). 14-digit codes: 0 observed
DPC_CODE_RE = re.compile(r"(?<![0-9０-９])([0-9０-９]{6})(?![0-9０-９])")
# Headings in the nursing / base-up attachment unrelated to medical care (cut by the source 【見出し】, not judged by the question body)
KANBE_EXCLUDE_TOPICS = ("調剤ベースアップ評価料", "歯科技工所ベースアップ支援料")
# The nursing / base-up attachment never writes a kubun number (0 of 56 questions). Bundle by the literal part-O kubun name (first one in the question body).
# `歯科外来・在宅ベースアップ評価料` / `訪問看護ベースアップ評価料` are not fee schedule kubun, so exclude them
KANBE_NAME_RULES = (
    (re.compile(r"看護職員処遇改善評価料"), "O000"),
    (re.compile(r"(?<!歯科)外来・在宅ベースアップ評価料\s*[（(]\s*[ⅠI１1]\s*[)）]"), "O001"),
    (re.compile(r"(?<!歯科)外来・在宅ベースアップ評価料\s*[（(]\s*[ⅡII２2]\s*[)）]"), "O002"),
    (re.compile(r"(?<!訪問看護)入院ベースアップ評価料"), "O003"),
)

# Part directories. Same letter + name as the fee schedule parts
PARTS = {
    "A": "A_基本診療料",
    "B": "B_医学管理等",
    "C": "C_在宅医療",
    "D": "D_検査",
    "E": "E_画像診断",
    "F": "F_投薬",
    "G": "G_注射",
    "H": "H_リハビリテーション",
    "I": "I_精神科専門療法",
    "J": "J_処置",
    "K": "K_手術",
    "L": "L_麻酔",
    "M": "M_放射線治療",
    "N": "N_病理診断",
    "O": "O_その他",
}
NO_KUBUN_DIR = "区分番号なし"

# If one node body exceeds this, split into children per heading (same line as BLOCK_SPLIT of the fee schedule)
BLOCK_SPLIT = 20_000

_DASH = "－−‐―-"
KUBUN_RE = re.compile(rf"([Ａ-Ｚ][０-９]{{3}}(?:[{_DASH}][０-９0-9]{{1,2}}){{0,2}})")
_Z2H = str.maketrans(
    "".join(chr(0xFF10 + i) for i in range(10)) + "".join(chr(0xFF21 + i) for i in range(26)),
    "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ",
)

# 「問N」 at line start (column 0). An indented 「問」 is a re-listing under （参考）, so not a question
# The DPC attachment has sub-numbers: `問１－１` / `問３－１－４` / `問 ６ － 31` (sometimes with spaces)
Q_RE = re.compile(r"^\x0c?問\s*([０-９0-9]+(?:\s*[－-]\s*[０-９0-9]+){0,2})(?:\s+|$)(.*)$")
# 「（答）」 is at column 0. Some are spaced out as `（ 答 ）` (e.g. sono07 問23)
A_RE = re.compile(r"^\x0c?[（(]\s*答\s*[)）]\s*(.*)$")
TOPIC_START_RE = re.compile(r"^\x0c?【(.*)$")
BESSO_RE = re.compile(r"^[\s\x0c]*[（(]別添\s*([０-９0-9]*)[)）]\s*$")
# Attachment headings are matched against a known set. A loose form like `\S+関係$` would take
# wrapped body lines (`…特別の関係`) as headings. Add new kinds of attachment to KNOWN_HEADINGS
# (the build fails if no known heading follows （別添N）, so nothing slips through silently)
KNOWN_HEADINGS = (
    "医科診療報酬点数表関係",
    "医科診療報酬点数表関係（ＤＰＣ）",
    "医科・歯科診療報酬点数表関係",
    "歯科診療報酬点数表関係",
    "調剤報酬点数表関係",
    "訪問看護療養費関係",
    "看護職員処遇改善評価料及びベースアップ評価料関係",
    "掲示事項関係",
)
HEADING_RE = re.compile(r"^[\s\x0c]*(" + "|".join(re.escape(h) for h in KNOWN_HEADINGS) + r")\s*$")
FOOTER_RE = re.compile(r"^[\s\x0c]*(?:[医歯調訪看ベ]+|DPC|ＤＰＣ)\s*[－\-‐]\s*[0-9０-９]+\s*$")
# Correction / abolition declarations on the cover (after NFKC + whitespace removal)
CORRECT_RE = re.compile(
    r"「疑義解釈資料の送付について\(その(\d+)\)」\(令和\d+年\d+月\d+日事務連絡\)"
    r"別添(\d+)の問([\d、及び問]+)について、別添(\d+)のとおり訂正します"
)
ABOLISH_RE = re.compile(
    r"「疑義解釈資料の送付について\(その(\d+)\)」\(令和\d+年\d+月\d+日事務連絡\)"
    r"別添(\d+)の問([\d、及び問]+)は、廃止します"
)

# Corrections are written as **redline markup** (deletion = red strikethrough, insertion = red underline).
# The pdftotext text layer **keeps the deleted characters** (`栄養量を有するし、見た目にも配慮した` = 「する」 is struck through).
# Strikethrough cannot be read as text, so for each correction **a human looks at the PDF** and registers the deleted string.
#
# 1. Corrections via an attachment of a later その N … (target その N, question) → [(text including the deletion, text after deletion)].
#    **An unregistered correction fails the build** (register insertion-only corrections with an empty list too)
CORRECTION_STRIKES: dict[tuple[int, str], list[tuple[str, str]]] = {
    (2, "26"): [],                      # その5 別添6: only the insertion of 「充実」
    (2, "143"): [("栄養量を有するし、見た目にも配慮した", "栄養量を有し、見た目にも配慮した")],  # その5 別添6
    (4, "14"): [],                      # その6 別添5: only the insertion of 「に係るいずれかの取組」
}
# 2. 「その N の一部訂正について」 (a standalone PDF) … the その N main body on the special page has been
#    replaced with the corrected version, so use the main body as-is and only add a note. Check that it was replaced:
#    (その N, question) → (text that **must** be in the main body, text that **must not** be = before correction)
TEISEI_CHECK = {
    (2, "104"): ("「Ｋ５１４－２」胸腔鏡下肺悪性腫瘍手術", "Ｋ１５４－２"),
}

# Kubun number typos in the source. When a question cites a kubun number absent from the notification, bundle it
# under the registered kubun (body stays as in the source; the node's "区分の根拠" says it is a typo). Unregistered = no kubun
KUBUN_TYPOS = {
    # その2 問27/28/31 「Ａ２０５－７」電子的診療情報連携体制整備加算 — the notification says Ａ２０７－５
    # (その12 問4 correctly writes 「Ａ２０７－５」)
    "A205-7": "A207-5",
}

TITLE_MAX = 60


@dataclass
class Entry:
    sono: int
    issued: str
    url: str
    besso: int
    qno: str  # "26" / "3-1-4" for DPC
    topic: str
    q: list[str]
    a: list[str]
    kubun_cited: list[str] = field(default_factory=list)
    kubun: str | None = None  # bundle target (a kubun that exists in the fee schedule)
    kubun_how: str = ""  # basis for the kubun (written into the body)
    corrected_by: dict | None = None
    abolished_by: dict | None = None
    heading: str = ""

    @property
    def key(self) -> tuple[int, int, str]:
        return (self.sono, self.besso, self.qno)

    @property
    def qkey(self) -> tuple[int, ...]:
        return tuple(int(x) for x in self.qno.split("-"))

    @property
    def label(self) -> str:
        """`その8 問1` / `その8 別添2 問1`. The medical / dental attachment shares question numbers with 別添1, so it names the attachment."""
        if self.besso == 1:
            return f"その{self.sono} 問{self.qno}"
        return f"その{self.sono} 別添{self.besso} 問{self.qno}"


@dataclass
class Correction:
    src_sono: int
    src_issued: str
    src_url: str
    src_label: str
    target_sono: int
    target_besso: int
    qnos: list[str]
    entries: list[Entry]


def norm_kubun(raw: str) -> str:
    s = raw.translate(_Z2H)
    return re.sub(rf"[{_DASH}]", "-", s)


def _nfkc_nospace(s: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s))


def _qnos(s: str) -> list[str]:
    return [x for x in re.findall(r"\d+", s)]

def _qno_norm(raw: str) -> str:
    return re.sub(r"\s+", "", raw.translate(_Z2H)).replace("－", "-")


# ---------------------------------------------------------------------------
# Text formatting
# ---------------------------------------------------------------------------

# A line starting with this is a candidate bullet / numbered item. Start a new line **only when the previous line ends a sentence** —
# a wrapped line may happen to start with `(２)の…` or `（11）のイ` (a reference to an item in the body)
_LIST_RE = re.compile(
    r"^(?:[（(]\s*[0-9０-９一二三四五六七八九十]+\s*[)）]|[0-9０-９]+[．.]\s|"
    r"[アイウエオカキクケコサシスセソ]\s|例[）)]|[０-９0-9]+回目|実施回\s)"
)
# A circled number starts an item even after a comma (`…とあるが、\n①「１」を算定した…`)
_CIRCLED_RE = re.compile(r"^[①-⑳]")
# Always start a new line (these almost never begin a wrapped line)
_HARD_LIST_RE = re.compile(r"^(?:[・※･]|[（(]例[)）]|[（(]参考[)）]|＜|【)")
# Sentence end. The line after one ending here may start a paragraph or item
_SENTENCE_END = "。」）:："
_SENTENCE_END_SOFT = _SENTENCE_END + "、"
_URL_RE = re.compile(r"https?://\S+$")
_NON_ASCII = r"[^\x00-\x7f]"
_INTER_CJK_SPACE = re.compile(rf"(?<={_NON_ASCII}) (?={_NON_ASCII})")
# Traces of justified text in pdftotext -layout: whitespace stretches only before 「 or （ (`ついて、    「特定機能病院`)
_JUSTIFY_GAP = re.compile(r"\s{2,}(?=[「（『])")
_TABLE_GAP = re.compile(r"\S\s{2,}\S")


def _clean_line(s: str) -> str:
    """Format one line. For spaced-out lines (`現 時 点 で は`) collapse single spaces between non-ASCII
    characters, and drop the justified whitespace before 「. Other runs of 2+ spaces (table alignment) are kept."""
    s = s.replace("\x0c", "").rstrip()
    s = _JUSTIFY_GAP.sub("", s)
    s = _INTER_CJK_SPACE.sub("", s)
    return s


def _is_table_row(s: str) -> bool:
    """Two or more gaps of 2+ spaces = a table row (a single gap is treated as a justification trace)."""
    return len(_TABLE_GAP.findall(s)) >= 2


def reflow(lines: list[str]) -> list[str]:
    """Rejoin wrapped lines into paragraphs.

    grep matches substrings, so a word split by the source wrapping like `抗\\n菌薬適正使用体制加算`
    would not match as-is. Japanese words cut at a line end can simply be concatenated (no space).
    Breaks are kept for (1) a bullet / numbered line start when the previous line ends a sentence,
    (2) table rows, (3) after a blank line when the previous line ends a sentence. **A page-break blank
    line is not a paragraph break** (a sentence continues across pages), hence the condition in (3).
    """
    out: list[str] = []
    blank = False
    for raw in lines:
        s = _clean_line(raw)
        if not s.strip():
            blank = True
            continue
        body = s.strip()
        prev = out[-1] if out else ""
        prev_ended = (not prev) or prev[-1] in _SENTENCE_END
        prev_ended_soft = (not prev) or prev[-1] in _SENTENCE_END_SOFT
        new_line = (
            not out
            or _is_table_row(body)
            or _is_table_row(prev)
            or _HARD_LIST_RE.match(body)
            or (_LIST_RE.match(body) and prev_ended)
            or (_CIRCLED_RE.match(body) and prev_ended_soft)
            or (blank and prev_ended)
            # The line after one ending with a URL is a separate line (`https://…\n診療所版Ｊ－ＳＩＰＨＥ：`)
            or bool(_URL_RE.search(prev))
        )
        if new_line:
            if blank and out and not _LIST_RE.match(body) and not _CIRCLED_RE.match(body) \
                    and not _is_table_row(body):
                out.append("")
            out.append(body)
        else:
            # Insert a space only where ASCII meets ASCII across the line break (`C2\nCryoBalloon`)
            joiner = " " if (prev[-1:].isascii() and prev[-1:].isalnum()
                             and body[:1].isascii() and body[:1].isalnum()) else ""
            out[-1] = prev + joiner + body
        blank = False
    return out


def _oneline(lines: list[str]) -> str:
    return re.sub(r"\s+", " ", " ".join(lines)).strip()


# ---------------------------------------------------------------------------
# Parsing the source
# ---------------------------------------------------------------------------


def split_bessos(text: str) -> tuple[list[str], list[tuple[int, str, list[str]]]]:
    """PDF text → (cover lines, [(attachment number, heading, lines)]).

    Attachment boundaries are found by the **「〜関係」 heading line**. The （別添N） marker
    is missing for 別添1 / 別添2 of その1 (heading only), and conversely the same （別添５）
    appears twice on the continued home-nursing pages, so the number is taken from the marker
    if present, else previous + 1.
    """
    lines = text.split("\n")
    heads: list[tuple[int, int, str]] = []  # (line number, attachment number, heading)
    for i, line in enumerate(lines):
        m = HEADING_RE.match(line)
        if not m:
            continue
        # Take （別添N） if it is within the previous 6 lines
        n = 0
        for j in range(max(0, i - 6), i):
            mb = BESSO_RE.match(lines[j])
            if mb and mb.group(1):
                n = int(mb.group(1).translate(_Z2H))
        if not n:
            n = (heads[-1][1] + 1) if heads else 1
        heads.append((i, n, m.group(1)))
    if not heads:
        sys.exit("no 「〜関係」 heading found at all")
    head_lines = {i for i, _n, _h in heads}
    for i, line in enumerate(lines):
        if BESSO_RE.match(line) and not any(j in head_lines for j in range(i + 1, i + 7)):
            # The same （別添５） may be repeated on the continued home-nursing pages = allowed inside the preceding attachment
            if not any(h[0] < i for h in heads):
                sys.exit(f"no known attachment heading right after {line.strip()} at line {i + 1} (review KNOWN_HEADINGS)")
    cover_end = heads[0][0]
    # The cover runs up to the first （別添１） marker (or up to the heading if there is no marker)
    for j in range(max(0, cover_end - 6), cover_end):
        if BESSO_RE.match(lines[j]):
            cover_end = j
            break
    cover = lines[:cover_end]
    out = []
    for k, (i, n, heading) in enumerate(heads):
        end = heads[k + 1][0] if k + 1 < len(heads) else len(lines)
        body = lines[i + 1 : end]
        # The marker line of the next attachment (（別添N）) ends up at the tail, so drop it
        while body and (BESSO_RE.match(body[-1]) or not body[-1].strip()):
            body.pop()
        out.append((n, heading, body))
    return cover, out


def parse_blocks(lines: list[str], *, meta: dict) -> list[Entry]:
    """Lines of one attachment → sequence of Entry. Split at 「問N」 in column 0, and divide question / answer at （答）."""
    entries: list[Entry] = []
    topic = ""
    chapter = ""  # Numbered heading of the DPC attachment. 【見出し】 under it is bundled as `chapter／heading`
    topic_open: list[str] | None = None
    cur: Entry | None = None
    cur_lines: list[str] = []

    def flush():
        nonlocal cur, cur_lines
        if cur is None:
            return
        q: list[str] = []
        a: list[str] | None = None
        for line in cur_lines:
            if a is None:
                m = A_RE.match(line)
                if m:
                    a = [m.group(1)]
                    continue
                q.append(line)
            else:
                a.append(line)
        if a is None:
            sys.exit(f"その{meta['sono']} 別添{meta['besso']} 問{cur.qno}: no （答）")
        cur.q = reflow(q)
        cur.a = reflow(a)
        if not cur.q or not cur.a:
            sys.exit(f"その{meta['sono']} 別添{meta['besso']} 問{cur.qno}: question or answer is empty")
        entries.append(cur)
        cur, cur_lines = None, []

    for raw in lines:
        line = raw.rstrip()
        if FOOTER_RE.match(line):
            continue
        if topic_open is not None:
            topic_open.append(line.strip())
            if "】" in line:
                topic = "".join(topic_open)
                topic = topic[: topic.index("】")]
                if chapter:
                    topic = f"{chapter}／{topic}"
                topic_open = None
            continue
        if meta["heading"] == DPC_HEADING:
            mc = DPC_CHAPTER_RE.match(line)
            if mc:
                flush()
                chapter = f"{int(mc.group(1).translate(_Z2H))}．{mc.group(2).strip()}"
                topic = chapter
                continue
        m = TOPIC_START_RE.match(line)
        if m:
            flush()
            rest = m.group(1)
            if "】" in rest:
                topic = rest[: rest.index("】")]
                if chapter:
                    topic = f"{chapter}／{topic}"
            else:
                topic_open = [rest]
            continue
        m = Q_RE.match(line)
        if m:
            flush()
            cur = Entry(
                sono=meta["sono"], issued=meta["issued"], url=meta["url"],
                besso=meta["besso"], qno=_qno_norm(m.group(1)),
                topic=topic, q=[], a=[], heading=meta["heading"],
            )
            cur_lines = [m.group(2)]
            continue
        if cur is not None:
            cur_lines.append(line)
    flush()
    return entries


def parse_cover(cover: list[str]) -> tuple[list[tuple], list[tuple]]:
    """Cover → (correction declarations, abolition declarations). **Fails if the number of declarations
    does not match the number parsed** (a new その N wording a correction / abolition differently must not pass silently)."""
    flat = _nfkc_nospace("".join(cover))
    corrects = [(int(a), int(b), _qnos(c), int(d)) for a, b, c, d in CORRECT_RE.findall(flat)]
    abolishes = [(int(a), int(b), _qnos(c)) for a, b, c in ABOLISH_RE.findall(flat)]
    n_c = flat.count("のとおり訂正します")
    n_a = flat.count("は、廃止します")
    if n_c != len(corrects) or n_a != len(abolishes):
        sys.exit(
            f"could not parse all correction / abolition declarations on the cover (corrections {len(corrects)} of {n_c} / "
            f"abolitions {len(abolishes)} of {n_a}):\n{flat[:600]}"
        )
    return corrects, abolishes


def load_all() -> tuple[dict, list[Entry], list[Correction], list[tuple], Counter]:
    manifest = json.loads((CACHE / "manifest.json").read_text(encoding="utf-8"))
    entries: list[Entry] = []
    corrections: list[Correction] = []
    abolishes: list[tuple[int, int, list[int], dict]] = []
    excluded = Counter()
    by_key: dict[tuple, Entry] = {}

    for item in manifest["items"]:
        # The source text layer contains Kangxi radical glyphs (「⽇本泌尿器科学会」 in その2).
        # Right after reading the pdftotext output = ahead of every path, so one pass here covers all nodes.
        # The verify (verify_gigi_kaishaku.py) comparison is NFKC on both sides, so it stays green.
        # `patches` / correction before/after are literal matches, so any registration written in
        # radical form must be fixed to the normal glyph (currently the 4 spots in K773-4 are not correction targets)
        text = fix_radicals((CACHE / f"{item['file']}.txt").read_text(encoding="utf-8"))
        meta_base = {"sono": item["n"], "issued": item["issued"], "url": item["url"]}
        if item["kind"] == "teisei":
            # Partial correction of 「その N」: after （別添） comes a reprint of the その N cover → 【見出し】問N
            idx = next(i for i, l in enumerate(text.split("\n")) if BESSO_RE.match(l))
            body = text.split("\n")[idx + 1 :]
            ents = parse_blocks(body, meta={**meta_base, "besso": 1, "heading": IKA_HEADING})
            corrections.append(Correction(
                src_sono=item["n"], src_issued=item["issued"], src_url=item["url"],
                src_label=item["label"], target_sono=item["n"], target_besso=1,
                qnos=[e.qno for e in ents], entries=ents,
            ))
            continue
        cover, bessos = split_bessos(text)
        c_decl, a_decl = parse_cover(cover)
        decl_by_besso = {d[3]: d for d in c_decl}
        for besso, heading, lines in bessos:
            if heading not in INCLUDE_HEADINGS:
                excluded[heading] += 1
                continue
            meta = {**meta_base, "besso": besso, "heading": heading}
            ents = parse_blocks(lines, meta=meta)
            if heading == KANBE_HEADING:
                drop = [e for e in ents if e.topic in KANBE_EXCLUDE_TOPICS]
                for e in drop:
                    excluded[f"看ベ別添の医科以外の見出し【{e.topic}】"] += 1
                ents = [e for e in ents if e.topic not in KANBE_EXCLUDE_TOPICS]
            if heading == IKA_HEADING and besso != 1:
                # A medical attachment other than 別添1 = a correction. Match against the cover declarations
                d = decl_by_besso.get(besso)
                if not d:
                    sys.exit(f"その{item['n']} 別添{besso} is medical but the cover has no correction declaration")
                if sorted(e.qno for e in ents) != sorted(d[2]):
                    sys.exit(f"その{item['n']} 別添{besso}: correction declaration {d[2]} does not match "
                             f"the contents {[e.qno for e in ents]}")
                corrections.append(Correction(
                    src_sono=item["n"], src_issued=item["issued"], src_url=item["url"],
                    src_label=item["label"], target_sono=d[0], target_besso=d[1],
                    qnos=d[2], entries=ents,
                ))
                continue
            for e in ents:
                if e.key in by_key:
                    sys.exit(f"duplicate question number: {e.key}")
                by_key[e.key] = e
                entries.append(e)
        # Cover correction declarations whose attachment is not medical (pharmacy etc.) are out of scope — count, do not fail
        for d in c_decl:
            if d[3] not in {b for b, h, _ in bessos if h == IKA_HEADING}:
                excluded[f"訂正 (対象外の別添: その{d[0]} 別添{d[1]})"] += 1
        for a in a_decl:
            abolishes.append((a[0], a[1], a[2], {
                "sono": item["n"], "issued": item["issued"], "url": item["url"],
                "label": item["label"],
            }))

    # Apply corrections to the main body
    for c in corrections:
        for ce in c.entries:
            tgt = by_key.get((c.target_sono, c.target_besso, ce.qno))
            if tgt is None:
                sys.exit(f"correction target not found: その{c.target_sono} 別添{c.target_besso} 問{ce.qno} "
                         f"(correction source その{c.src_sono})")
            if c.src_sono == c.target_sono:
                # 「その N の一部訂正について」 (a standalone PDF). **Its content is errata redline**,
                # with deleted and inserted characters side by side in the text layer (`「Ｋ１５４－２Ｋ５１４－２」`).
                # The その N main body on the special page has been replaced with the corrected version, so keep
                # the main body text and only add a correction note. TEISEI_CHECK verifies mechanically that it was replaced
                chk = TEISEI_CHECK.get((c.target_sono, ce.qno))
                if chk is None:
                    sys.exit(f"partial correction of その{c.target_sono} 問{ce.qno} is not in TEISEI_CHECK "
                             "(check by eye that the main PDF is corrected, then register it)")
                text = "".join(tgt.q + tgt.a)
                must, must_not = chk
                if must not in text or must_not in text:
                    sys.exit(f"その{c.target_sono} 問{ce.qno}: main PDF is not corrected "
                             f"(expected {must!r} present / {must_not!r} absent)")
            else:
                strikes = CORRECTION_STRIKES.get((c.target_sono, ce.qno))
                if strikes is None:
                    sys.exit(f"correction of その{c.target_sono} 問{ce.qno} (その{c.src_sono} attachment) is "
                             "not in CORRECTION_STRIKES (look at the strikethrough in the PDF and register it)")
                q, a = ce.q, ce.a
                for before, after in strikes:
                    joined = "\n".join(q) + "\n\n" + "\n".join(a)
                    if joined.count(before) != 1:
                        sys.exit(f"その{c.target_sono} 問{ce.qno}: struck-through text {before!r} occurs "
                                 f"{joined.count(before)} times in the correction (expected once)")
                    q = [l.replace(before, after) for l in q]
                    a = [l.replace(before, after) for l in a]
                tgt.q, tgt.a, tgt.topic = q, a, (ce.topic or tgt.topic)
            tgt.corrected_by = {"sono": c.src_sono, "issued": c.src_issued,
                                "url": c.src_url, "label": c.src_label}
    # Abolitions
    applied_abolish = []
    for t_sono, t_besso, qnos, src in abolishes:
        for qno in qnos:
            tgt = by_key.get((t_sono, t_besso, qno))
            if tgt is None:
                # Out of scope if the target is a non-medical attachment (pharmacy etc.)
                excluded[f"廃止 (対象外の別添: その{t_sono} 別添{t_besso})"] += 1
                continue
            tgt.abolished_by = src
            applied_abolish.append((t_sono, t_besso, qno, src))
    return manifest, entries, corrections, applied_abolish, excluded


# ---------------------------------------------------------------------------
# Fee schedule kubun → path
# ---------------------------------------------------------------------------


DPC_MAP: dict[str, str] = {}


def dpc_map() -> dict[str, str]:
    """Top 6 digits of a diagnosis group → reference written in the body `{{DOC_BASE}}/診療報酬/DPC点数表R08/<MDC>/<6桁>_<名称>.md`
    (existing leaves only)."""
    out: dict[str, str] = {}
    if not DPC_ROOT.exists():
        sys.exit(f"{DPC_ROOT} not found (the DPC fee schedule must be built first)")
    for p in DPC_ROOT.rglob("*.md"):
        m = re.match(r"(\d{6})_", p.name)
        if m:
            out[m.group(1)] = doc_ref(str(p.relative_to(CATEGORY.parent)), is_dir=False)
    return out


def tensuhyo_map() -> dict[str, tuple[str, str]]:
    """Kubun key (`A000-2`) → (path, title, ref). Only the kubun nodes themselves (`A000_…`) are taken;
    `告示` / `コード表` children split out of blocks are excluded. path is `/<category>/…` (node identifier),
    ref is the canonical form written in the body (leaf `{{DOC_BASE}}/….md` / directory node with children `{{DOC_BASE}}/…/`)."""
    out: dict[str, tuple[str, str, str]] = {}
    for sec in TENSUHYO_SECTIONS:
        root = CATEGORY / sec
        if not root.exists():
            sys.exit(f"{root} not found (run build_shinryo_hoshu.py first)")
        for p in root.rglob("*.md"):
            node = p.parent if p.name == "_index.md" else p.with_suffix("")
            m = re.match(r"([A-Z]\d{3}(?:-\d{1,2}){0,2})_", node.name)
            if not m:
                continue
            fm = p.read_text(encoding="utf-8").split("---", 2)[1]
            t = re.search(r"^title: '?(.*?)'?$", fm, re.M)
            path = "/" + str(node.relative_to(CATEGORY.parent))
            out[m.group(1)] = (path, t.group(1) if t else node.name, doc_ref(path, is_dir=p.name == "_index.md"))
    return out


def _title_key(s: str) -> str:
    return _nfkc_nospace(s)


def _title_key_bare(s: str) -> str:
    """Key with the trailing parenthetical (`（１日につき）` / `（入院初日）`) dropped. The parenthetical in a
    kubun name is the billing unit, which the source 【見出し】 omits (`【地域医療体制確保加算】` = A252).
    Where the parenthetical is part of the name (`診療情報提供料（Ⅰ）`), dropping it makes B009/B010 collide,
    so **try the exact match first and use this one only when unique**."""
    return re.sub(r"\([^()]*\)$", "", _title_key(s))


def assign_kubun(entries: list[Entry], tmap: dict[str, tuple[str, str]]) -> Counter:
    by_title: dict[str, list[str]] = defaultdict(list)
    by_title_bare: dict[str, list[str]] = defaultdict(list)
    for k, (_p, t, _r) in tmap.items():
        by_title[_title_key(t)].append(k)
        by_title_bare[_title_key_bare(t)].append(k)
    how = Counter()
    # The source 【見出し】 bundles consecutive questions within the same その N (a reference to an earlier
    # question like 「問１について、…」 is always within the same heading). When the question itself has no
    # kubun, inherit **the first kubun that appeared in the same heading bundle** — so the bundle is not scattered across nodes
    groups: dict[tuple[int, int, str], list[Entry]] = defaultdict(list)
    for e in entries:
        groups[(e.sono, e.besso, e.topic)].append(e)
    group_kubun: dict[tuple[int, int, str], tuple[str, int]] = {}
    for e in entries:
        if e.heading == DPC_HEADING:
            how["DPC (見出し単位)"] += 1
            continue
        qtext = "".join(e.q)
        cited = []
        for raw in KUBUN_RE.findall(qtext):
            k = norm_kubun(raw)
            if k not in cited:
                cited.append(k)
        e.kubun_cited = cited
        hit = next((k for k in cited if k in tmap), None)
        typo = next((k for k in cited if k in KUBUN_TYPOS), None)
        if hit:
            e.kubun = hit
            e.kubun_how = "問の本文に出る区分番号"
            how["問の区分番号"] += 1
        elif e.heading == KANBE_HEADING:
            found = sorted(
                ((m.start(), k) for rx, k in KANBE_NAME_RULES if k in tmap for m in [rx.search(qtext)] if m),
            )
            if found:
                hit = found[0][1]
                e.kubun = hit
                e.kubun_how = (f"問の本文に区分番号は無い。ベースアップ評価料の別添は区分番号を書かないので、"
                               f"問の本文に最初に出る O 部の区分名 ({tmap[hit][1]}) で束ねた")
                how["看ベ: 区分名の字面"] += 1
        elif typo:
            hit = KUBUN_TYPOS[typo]
            e.kubun = hit
            e.kubun_how = (f"問の本文の「{typo}」は告示に無い区分番号 (原本の誤記と判断し、"
                           f"{hit} に束ねた。本文は原本のまま)")
            how["原本の誤記を読み替え"] += 1
        if hit:
            gk = (e.sono, e.besso, e.topic)
            if e.topic and gk not in group_kubun:
                group_kubun[gk] = (hit, e.qno)
    for e in entries:
        if e.kubun or e.heading == DPC_HEADING:
            continue
        gk = (e.sono, e.besso, e.topic)
        if gk in group_kubun:
            e.kubun, src_q = group_kubun[gk]
            e.kubun_how = f"問の本文に区分番号は無い。同じ見出し【{e.topic}】の問{src_q}が出す区分に束ねた"
            how["同じ見出しの問の区分"] += 1
            continue
        ks = by_title.get(_title_key(e.topic), [])
        if len(ks) != 1:
            ks = by_title_bare.get(_title_key_bare(e.topic), [])
        if len(ks) == 1:
            e.kubun = ks[0]
            e.kubun_how = f"問の本文に区分番号は無い。見出し【{e.topic}】が区分名と一致"
            how["見出し = 区分名"] += 1
            continue
        how["区分なし"] += 1
    return how


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _yaml_str(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _slug(s: str, limit: int = 40) -> str:
    """Filename. As on the fee schedule side, full-width characters stay (no NFKC)."""
    s = re.sub(r'[/\\:*?"<>|\r\n\t]', "", s).strip()
    s = re.sub(r"\s+", "_", s)
    return safe_segment((s[:limit] or "無題").rstrip("_"))  # URL-breaking ASCII → full-width


def _q_title(e: Entry) -> str:
    s = _oneline(e.q)
    if len(s) <= TITLE_MAX:
        return s
    cut = s[:TITLE_MAX]
    idx = max((cut.rfind(c) for c in "。、」）)"), default=-1)
    if idx >= int(TITLE_MAX * 0.66):
        cut = cut[: idx + 1]
    return cut.rstrip() + "…"


def render_entry(e: Entry, tmap: dict[str, tuple[str, str]], *, own_kubun: str | None) -> str:
    lines = []
    head = f"### {e.label}"
    if e.abolished_by:
        head += " 【廃止】"
    elif e.corrected_by:
        head += " 【訂正あり】"
    lines.append(head)
    lines.append("")
    meta = [f"発出: {e.issued} ({e.heading}{'' if e.besso == 1 else f' 別添{e.besso}'})"]
    if e.topic:
        meta.append(f"原本の見出し: 【{e.topic}】")
    if own_kubun and e.kubun_how and not e.kubun_how.startswith("問の本文に出る"):
        meta.append(f"区分の根拠: {e.kubun_how}")
    others = [k for k in e.kubun_cited if k in tmap and k != own_kubun]
    if others:
        meta.append("問に出る他の区分: " + " / ".join(f"`{tmap[k][2]}`" for k in others))
    if e.heading == DPC_HEADING:
        codes = []
        for raw in DPC_CODE_RE.findall("".join(e.q)):
            c = raw.translate(_Z2H)
            if c in DPC_MAP and c not in codes:
                codes.append(c)
        if codes:
            meta.append("問に出る診断群分類: " + " / ".join(f"`{DPC_MAP[c]}`" for c in codes))
    lines.append("- " + "\n- ".join(meta))
    lines.append("")
    if e.abolished_by:
        ab = e.abolished_by
        lines.append(
            f"> **この問は廃止された** — その{ab['sono']} ({ab['issued']}) の表紙で"
            f"「廃止します」と宣言されている。以下は廃止前の内容で、**現行の解釈として"
            f"使わないこと**。原本: {ab['url']}"
        )
        lines.append("")
    elif e.corrected_by:
        cb = e.corrected_by
        src = (f"「疑義解釈資料の送付について（その{cb['sono']}）」の一部訂正について ({cb['issued']})"
               if cb["sono"] == e.sono else f"その{cb['sono']} ({cb['issued']}) の別添")
        lines.append(
            f"> **訂正後の内容**: {src}で一部訂正されている。"
            f"以下は訂正後の本文 (訂正前は載せていない)。訂正元: {cb['url']}"
        )
        lines.append("")
    lines.append("**問**")
    lines.append("")
    lines.extend(e.q)
    lines.append("")
    lines.append("**答**")
    lines.append("")
    lines.extend(e.a)
    lines.append("")
    return "\n".join(lines)


def _as_of(manifest: dict) -> tuple[int, str, str]:
    return manifest["latest"]["n"], manifest["latest"]["issued"], manifest["retrieved_at"]


def _leaf_footer(manifest: dict) -> str:
    n, issued, retrieved = _as_of(manifest)
    return (
        f"> 収録時点: **その{n} ({issued} 発出) まで** ({retrieved} 取得)。疑義解釈は継続発出中で、"
        f"これより後の その N はここに無い。最新は厚生労働省の改定特設ページ {SOURCE} で確認すること。"
        f"問の答は施設基準通知・留意事項通知の解釈であり、点数と算定要件の正典は "
        f"`{DOC_BASE}/診療報酬/医科点数表R08_…` の各区分ノード、施設基準の本文は `{DOC_BASE}/診療報酬/施設基準R08_…`、"
        f"当院の専任・専従の登録名簿は `{SISETSU_TODOKEDE}` (届出の有無ではない)。"
    )


def write_tree(manifest: dict, entries: list[Entry], corrections: list[Correction],
               abolished: list, excluded: Counter, tmap: dict, how: Counter) -> dict:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    n_latest, issued_latest, retrieved = _as_of(manifest)
    sonos = [i for i in manifest["items"] if i["kind"] == "sono"]
    footer = _leaf_footer(manifest)

    # Bundling
    by_kubun: dict[str, list[Entry]] = defaultdict(list)
    by_topic: dict[str, list[Entry]] = defaultdict(list)
    by_dpc: dict[str, list[Entry]] = defaultdict(list)
    for e in entries:
        if e.heading == DPC_HEADING:
            by_dpc[e.topic or "(見出しなし)"].append(e)
        elif e.kubun:
            by_kubun[e.kubun].append(e)
        else:
            by_topic[e.topic or "(見出しなし)"].append(e)
    for lst in by_dpc.values():
        lst.sort(key=lambda e: (e.sono, e.besso, e.qkey))
    for lst in list(by_kubun.values()) + list(by_topic.values()):
        lst.sort(key=lambda e: (e.sono, e.besso, e.qkey))

    stats = {"nodes": 0, "split": 0, "max_body": 0, "dirs": Counter()}

    def leaf_fm(title: str, kubun: str, section: str, extra: dict | None = None) -> str:
        fm = [
            "---",
            f"title: {_yaml_str(title)}",
            f"kubun: {_yaml_str(kubun)}",
            f"section: {_yaml_str(section)}",
            f"source: {SOURCE}",
            f"retrieved_at: '{retrieved}'",
        ]
        if extra:
            fm.append("fields:")
            for k, v in extra.items():
                fm.append(f"  {k}: {v}")
        fm.append("---")
        return "\n".join(fm) + "\n\n"

    CHILD_FIELDS = {"kubun": "str", "section": "str", "source?": "str", "retrieved_at?": "date"}

    def write_bundle(d: Path, name: str, title: str, kubun: str, section: str,
                     intro: str, ents: list[Entry], own_kubun: str | None) -> None:
        """1 bundle = 1 node. Beyond BLOCK_SPLIT, split into child nodes per heading (topic)."""
        rendered = [render_entry(e, tmap, own_kubun=own_kubun) for e in ents]
        body = intro + "\n\n" + "\n---\n\n".join(rendered) + "\n\n" + footer + "\n"
        if len(body) <= BLOCK_SPLIT:
            (d / f"{name}.md").write_text(leaf_fm(title, kubun, section) + body, encoding="utf-8")
            stats["nodes"] += 1
            stats["max_body"] = max(stats["max_body"], len(body))
            return
        # Children per heading (split by issue if there is only one heading)
        groups: dict[str, list[Entry]] = defaultdict(list)
        keyf = (lambda e: e.topic or "(見出しなし)") if len({e.topic for e in ents}) > 1 \
            else (lambda e: f"その{e.sono}")
        for e in ents:
            groups[keyf(e)].append(e)
        nd = d / name
        nd.mkdir()
        child_lines = []
        used: set[str] = set()
        for gname, ge in groups.items():
            slug = _slug(gname, 50)
            while slug in used:
                slug += "_"
            used.add(slug)
            child_lines.append(f"- `{doc_ref(str((nd / slug).relative_to(CATEGORY.parent)), is_dir=False)}` — "
                               f"{gname} ({len(ge)} 問: {' / '.join(e.label for e in ge)})")
            cbody = (
                f"{title} の疑義解釈のうち **{gname}** の分 ({len(ge)} 問)。親ノード "
                f"`{doc_ref(str(nd.relative_to(CATEGORY.parent)), is_dir=True)}` に全体の一覧がある。\n\n"
                + "\n---\n\n".join(render_entry(e, tmap, own_kubun=own_kubun) for e in ge)
                + "\n\n" + footer + "\n"
            )
            (nd / f"{slug}.md").write_text(
                leaf_fm(f"{title} — {gname}", kubun, section) + cbody, encoding="utf-8")
            stats["nodes"] += 1
            stats["max_body"] = max(stats["max_body"], len(cbody))
        ibody = (
            intro + f"\n\n**問が {len(ents)} 本と多いので、原本の見出しごとに子ノードへ分けてある。"
            f"このノードには問の本文は載っていない。** 子を read すること:\n\n"
            + "\n".join(child_lines) + "\n\n" + footer + "\n"
        )
        (nd / "_index.md").write_text(
            leaf_fm(title, kubun, section, CHILD_FIELDS) + ibody, encoding="utf-8")
        stats["nodes"] += 1
        stats["split"] += 1

    # --- Part directories + kubun nodes
    parts_used: dict[str, list[str]] = defaultdict(list)
    for k in by_kubun:
        parts_used[k[0]].append(k)
    for letter, ks in sorted(parts_used.items()):
        dname = PARTS[letter]
        d = OUT / dname
        d.mkdir()
        ks.sort(key=lambda k: (k[:4], [int(x) for x in k[5:].split("-")] if len(k) > 4 else []))
        rows = []
        for k in ks:
            path, title, ref = tmap[k]
            ents = by_kubun[k]
            name = Path(path).name  # same basename as on the fee schedule side
            n_ab = sum(1 for e in ents if e.abolished_by)
            section = ("第1章 基本診療料" if letter == "A" else f"第2章 特掲診療料 {dname}")
            intro = (
                f"令和8年度診療報酬改定 疑義解釈 (厚生労働省保険局医療課 事務連絡) のうち、"
                f"医科点数表 「{k} {title}」 に関する問を発出順に束ねたもの ({len(ents)} 問"
                + (f"、うち廃止 {n_ab}" if n_ab else "") + ")。\n\n"
                f"点数・注・留意事項通知の本文は `{ref}` にある (このノードは疑義解釈だけ)。"
                f"問の区分番号は問の本文から機械的に読んだもので、複数の区分に触れる問は"
                f"**最初に出た区分**の下にある (他の区分は各問の「問に出る他の区分」に path で示す)。"
            )
            write_bundle(d, name, f"{k} {title} の疑義解釈", k, section, intro, ents, k)
            rows.append(f"- `{name}` … {len(ents)} 問" + (f" (廃止 {n_ab})" if n_ab else ""))
        stats["dirs"][dname] = len(ks)
        kind = "基本診療料" if letter == "A" else "特掲診療料"
        (d / "_index.md").write_text(
            leaf_fm(f"{dname} の疑義解釈", f"{ks[0]}〜{ks[-1]}",
                    "第1章 基本診療料" if letter == "A" else f"第2章 特掲診療料 {dname}",
                    CHILD_FIELDS)
            + f"医科点数表 {dname} の区分に関する疑義解釈。**1 ノード = 1 区分**で、ノード名は "
            f"`{doc_ref(f'/診療報酬/医科点数表R08_{kind}', is_dir=True)}` 側の区分ノードと同じ。"
            f"区分 {len(ks)} / 問 {sum(len(by_kubun[k]) for k in ks)}。\n\n" + "\n".join(rows)
            + "\n\n" + footer + "\n",
            encoding="utf-8",
        )
        stats["nodes"] += 1

    # --- No kubun number
    d = OUT / NO_KUBUN_DIR
    d.mkdir()
    rows = []
    used: set[str] = set()
    for topic, ents in sorted(by_topic.items(), key=lambda kv: (kv[1][0].sono, kv[1][0].qkey)):
        slug = _slug(topic, 50)
        while slug in used:
            slug += "_"
        used.add(slug)
        n_ab = sum(1 for e in ents if e.abolished_by)
        intro = (
            f"令和8年度診療報酬改定 疑義解釈 (厚生労働省保険局医療課 事務連絡) のうち、"
            f"原本の見出し 【{topic}】 の下にあって**問の本文に医科点数表の区分番号が出ない**問 "
            f"({len(ents)} 問" + (f"、うち廃止 {n_ab}" if n_ab else "") + ")。"
            f"施設基準の届出・様式・通則など区分をまたぐ話が多い。"
            f"同じ見出しでも問に区分番号が出るものは部ディレクトリ (`A_基本診療料` 等) の区分ノードにある。"
        )
        write_bundle(d, slug, f"【{topic}】 の疑義解釈", "-", "区分番号なし", intro, ents, None)
        rows.append(f"- `{slug}` … {len(ents)} 問 ({' / '.join(e.label for e in ents)})")
    stats["dirs"][NO_KUBUN_DIR] = len(by_topic)
    (d / "_index.md").write_text(
        leaf_fm("区分番号なしの疑義解釈 (総論・届出・様式等)", "-", "区分番号なし", CHILD_FIELDS)
        + f"問の本文に医科点数表の区分番号が出ない問を、原本の 【見出し】 ごとに束ねたもの "
        f"(見出し {len(by_topic)} / 問 {sum(len(v) for v in by_topic.values())})。"
        f"施設基準の届出手続き・様式・経過措置・選定療養・入院時食事療養 (特別食加算) など。\n\n"
        f"**区分名で探すなら先に部ディレクトリを見る。** ここに無い = その区分の疑義解釈が無い、ではない "
        f"(区分番号を問に書いていないだけのことがある)。`{SECTION_REF}` 全体を grep すること。\n\n"
        + "\n".join(rows) + "\n\n" + footer + "\n",
        encoding="utf-8",
    )
    stats["nodes"] += 1

    # --- DPC (attachment 「医科診療報酬点数表関係（ＤＰＣ）」). 1 node = 1 attachment heading
    d = OUT / DPC_DIR
    d.mkdir()
    rows = []
    used = set()
    n_dpc_codes = 0
    for topic, ents in sorted(by_dpc.items(), key=lambda kv: (kv[1][0].sono, kv[1][0].qkey)):
        slug = _slug(topic, 50)
        m = re.match(r"(\d+)．(.*)", slug)
        if m:
            slug = f"{int(m.group(1)):02d}_{m.group(2)}"
        while slug in used:
            slug += "_"
        used.add(slug)
        n_dpc_codes += sum(1 for e in ents if "問に出る診断群分類" in render_entry(e, tmap, own_kubun=None))
        intro = (
            f"令和8年度診療報酬改定 疑義解釈 (厚生労働省保険局医療課 事務連絡) の別添「医科診療報酬点数表関係（ＤＰＣ）」"
            f"のうち、見出し 「{topic}」 の下の問 ({len(ents)} 問)。DPC (診断群分類別包括評価) の制度・算定・"
            f"請求の Q&A で、**問の本文に診断群分類の番号が出るものは各問に分類ノードの path を示してある** "
            f"(`{DPC_REF}` 側。分類ごとの点数・定義はそちら、DPC 留意事項通知の本文は "
            f"`{DPC_TSUCHI_REF}`)。"
        )
        write_bundle(d, slug, f"DPC 疑義解釈 — {topic}", "-", "DPC", intro, ents, None)
        rows.append(f"- `{slug}` … {len(ents)} 問 ({ents[0].label} 〜 {ents[-1].label})")
    stats["dirs"][DPC_DIR] = len(by_dpc)
    (d / "_index.md").write_text(
        leaf_fm("DPC の疑義解釈 (医科診療報酬点数表関係（ＤＰＣ）の別添)", "-", "DPC", CHILD_FIELDS)
        + f"別添「医科診療報酬点数表関係（ＤＰＣ）」の問を、別添の見出し (その2 は `１．ＤＰＣ対象病院の基準について` の"
        f"番号見出し、その9 は 【診断群分類】) ごとに束ねたもの (見出し {len(by_dpc)} / 問 "
        f"{sum(len(v) for v in by_dpc.values())})。問番号は `問３－１－４` のように枝番付きで、"
        f"見出しの番号が先頭に付く。\n\n"
        f"**診断群分類の番号 (上 6 桁) が問に出る問は、各問の「問に出る診断群分類:」に "
        f"`{DOC_BASE}/診療報酬/DPC点数表R08/<MDC>/<6桁>_<名称>` の path を示してある** (問 {n_dpc_codes} 本)。"
        f"分類の点数・定義・手術処置等の定義はそちらを read すること。分類が読めない問 (制度一般・"
        f"係数・請求手順) は見出しのノードだけで完結する。\n\n" + "\n".join(rows) + "\n\n" + footer + "\n",
        encoding="utf-8",
    )
    stats["nodes"] += 1

    # --- Section _index.md
    sono_rows = []
    per_sono = Counter(e.sono for e in entries)
    for it in sonos:
        n_c = sum(1 for c in corrections if c.src_sono == it["n"] and c.target_sono != it["n"])
        note = ""
        self_c = [c for c in corrections if c.src_sono == it["n"] and c.target_sono == it["n"]]
        if self_c:
            note = f" ({self_c[0].src_issued} に一部訂正あり — 反映済み)"
        elif n_c:
            note = f" (後の その N の訂正 {n_c} 件をここに反映)"
        if any(c.src_sono == it["n"] and c.target_sono != it["n"] for c in corrections):
            note += " (過去の その N の訂正を含む — 訂正先に反映済み)"
        sono_rows.append(f"| その{it['n']} | {it['issued']} | {per_sono.get(it['n'], 0)} | {it['url']}{note} |")
    n_ab = len(abolished)
    split_note = (
        "- 問が多い区分は原本の見出しごとに子ノードへ分けてある (親が子の path を名乗る)"
        if stats["split"] else
        "- 1 区分の問は最大でも 2 桁本・1 万字強なので、区分ノードを子に分けてはいない (全問がそのノードにある)"
    )
    parts_rows = "\n".join(
        f"- `{PARTS[l]}` … 区分 {len(parts_used[l])} / 問 {sum(len(by_kubun[k]) for k in parts_used[l])}"
        for l in sorted(parts_used)
    )
    sec_body = f"""---
title: {_yaml_str(f'疑義解釈 (令和8年度診療報酬改定・医科) その1〜その{n_latest}')}
tensuhyo: {_yaml_str(TENSUHYO)}
publisher: {PUBLISHER}
applies_from: '{APPLIES_FROM}'
source: {SOURCE}
retrieved_at: '{retrieved}'
fields:
  kubun: str
  section: str
  source?: str
  retrieved_at?: date
---

厚生労働省保険局医療課が令和8年度診療報酬改定について発出した**疑義解釈資料 (事務連絡) の
Q&A** のうち、**医科診療報酬点数表関係の別添**を収録したもの。告示・通知 (`{DOC_BASE}/診療報酬/医科点数表R08_…`)
の条文をどう読むかを「問 / 答」で示した一次資料で、**点数表の本文には統合されない**。

## 収録時点 — ここが一番大事

- **その1 ({sonos[0]['issued']}) 〜 その{n_latest} ({issued_latest}) まで** を {retrieved} に取得。
  疑義解釈は**改定後も継続して発出される** (2〜3 週に 1 本)。**これより後の その N はここに無い。**
  最新は改定特設ページ {SOURCE} の「３．疑義解釈・訂正通知」で確認すること
- 一部訂正 (「その N」の一部訂正について / 後の その N の別添による訂正) は**訂正後の内容を本体に反映**し、
  該当する問に「訂正後の内容」と訂正元を注記してある (訂正前の本文は載せていない)
- 後の その N の表紙で「廃止します」と宣言された問は消さず **【廃止】** を付けて残してある ({n_ab} 問)。
  廃止前の内容を現行の解釈として使わないこと

| 発出回 | 発出日 | 医科の問 | 原本 |
|---|---|---:|---|
{chr(10).join(sono_rows)}

医科の問 **{len(entries)} 本** (訂正 {sum(len(c.entries) for c in corrections)} 件反映・廃止 {n_ab} 件)。

## 収録していないもの

- **歯科・調剤・訪問看護の別添** (歯科診療報酬点数表関係 / 調剤報酬点数表関係 / 訪問看護療養費関係)
- **看護職員処遇改善評価料及びベースアップ評価料関係の別添のうち、見出しが 【調剤ベースアップ評価料】
  【歯科技工所ベースアップ支援料】 の問** (薬局・歯科技工所だけの話)。この別添の残り (医科・歯科・訪問看護・
  薬局に共通する問) は収録している — 下記
- 掲示事項関係の別添、「療養の給付と直接関係ないサービス等の取扱いに関する疑義解釈資料」(保険外負担の話)、
  「改定関連通知及び官報掲載事項の一部訂正について」(告示・通知そのものの正誤表)

## 隣のセクション — 問が引用するものはここにある

問の多くが施設基準通知 (「基本診療料の施設基準等及びその届出に関する手続きの取扱いについて」等) を引用する。
その本文は同じカテゴリの別セクションに入っている:

- `{SISETSU_KIHON_REF}` … 基本診療料の施設基準通知。**1 ノード = 通知の「第N」= 1 施設基準**
- `{SISETSU_TOKKEI_REF}` … 特掲診療料の施設基準通知。点数表の部で切ってあり、K (手術) は款まで
- `{SISETSU_TODOKEDE}` … **当院の専任・専従の登録名簿** (施設基準に登録されている職員の一覧)。
  **届出の有無そのものではない**ので、「当院で算定できるか」はこれだけで断定しない
- `{TENSU_KIHON_REF}` / `…_特掲診療料` … 点数・注・留意事項通知 (各区分ノード)
- `{DPC_REF}` … DPC 電子点数表 (1 ノード = 1 診断群分類 `<MDC>/<6桁>_<名称>`) と DPC 留意事項通知。
  `{DPC_DIR}/` の問はここを名乗る

## ディレクトリ構成 — 1 ノード = 1 区分番号

問は発出回 (その N) をまたいで同じ区分に散っているので、**発出回でなく医科点数表の区分番号で束ねた**。
ノード名は `{TENSU_KIHON_REF}` / `…_特掲診療料` 側の区分ノードと同じ
(`A000_初診料`)。部でディレクトリを 1 段切ってある:

{parts_rows}
- `{NO_KUBUN_DIR}` … 問の本文に区分番号が出ない問を原本の 【見出し】 ごとに束ねたもの
  (見出し {len(by_topic)} / 問 {sum(len(v) for v in by_topic.values())})。届出手続き・様式・通則・選定療養・特別食加算など
- `{DPC_DIR}` … 別添「医科診療報酬点数表関係（ＤＰＣ）」の全問を別添の見出しごとに束ねたもの
  (見出し {len(by_dpc)} / 問 {sum(len(v) for v in by_dpc.values())})。診断群分類の番号が問に出るものは
  各問が `{DOC_BASE}/診療報酬/DPC点数表R08/…` の分類ノードの path を名乗る

- 区分は**問の本文** (答ではない) に出る 「Ａ０００」 形の区分番号で決めている。複数の区分に触れる問は
  **最初に出た区分**の下にあり、各問の「問に出る他の区分」に残りの path を示す
- 問に区分番号が無くても、原本の 【見出し】 が医科点数表の区分名と一致するものはその区分の下にある
- **看護職員処遇改善評価料及びベースアップ評価料関係の別添** (毎回 別添2) は医科点数表の O 部
  (O000 看護職員処遇改善評価料 / O001・O002 外来・在宅ベースアップ評価料（Ⅰ）（Ⅱ） / O003 入院ベースアップ評価料)
  の疑義解釈にあたるが、**問の本文が区分番号を書かず、歯科・訪問看護・薬局の同名の評価料と一緒に語る**。
  問の本文に最初に出る O 部の区分名の字面で束ね (歯科外来・訪問看護の評価料は除く)、名前も出ない問は
  同じ見出しの問に倣う。どの区分にも付かない問 (共通事項・入院基本料等の減算) は `{NO_KUBUN_DIR}`。
  各問の「発出:」行が別添名を、見出しが `### そのN 別添2 問M` を名乗る
- 各問の見出しは `### そのN 問M` で、原本の発出回と問番号をそのまま使っている
  (原本を引くときは「疑義解釈資料の送付について（その N）別添１ 問 M」)。医科・歯科共通の別添は
  別添1 と問番号が重なるので `### そのN 別添K 問M` と別添番号を名乗る
{split_note}

## 使い方

1. 区分番号が分かっているなら部ディレクトリ → 区分ノードを read する (点数表側の同名ノードと対)
2. 分からなければ `{SECTION_REF}` を grep する。問の本文は原本の折り返しを戻してあるので語が途中で切れていない
3. **答だけで算定可否を断定しない。** 答は告示・通知・施設基準通知の解釈で、点数と算定要件の正典は医科点数表の各区分ノード、
   施設基準の本文は `{DOC_BASE}/診療報酬/施設基準R08_…` (上記「隣のセクション」)

## ライセンス・出典

厚生労働省ホームページのコンテンツ (PDL1.0 準拠、カテゴリ `{CATEGORY_REF}` の `_index.md` 参照)。
原本 PDF のテキスト層を機械抽出し、折り返しを戻して問ごとに区分番号で並べ替えた**加工物**。
"""
    (OUT / "_index.md").write_text(sec_body, encoding="utf-8")
    stats["nodes"] += 1
    return stats


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stats", action="store_true", help="only print statistics, write no nodes")
    args = ap.parse_args()

    manifest, entries, corrections, abolished, excluded = load_all()
    tmap = tensuhyo_map()
    DPC_MAP.update(dpc_map())
    how = assign_kubun(entries, tmap)

    per_sono = Counter(e.sono for e in entries)
    print("medical questions (別添1 + medical/dental):", len(entries), dict(sorted(per_sono.items())), file=sys.stderr)
    print("kubun assignment:", dict(how), file=sys.stderr)
    print("corrections:", [(f"その{c.src_sono}→その{c.target_sono} 問{c.qnos}") for c in corrections], file=sys.stderr)
    print("abolished:", [(f"その{s} 別添{b} 問{q} (その{src['sono']})") for s, b, q, src in abolished], file=sys.stderr)
    print("excluded attachments / declarations:", dict(excluded), file=sys.stderr)

    by_kubun = Counter(e.kubun for e in entries if e.kubun)
    topics = Counter(e.topic for e in entries if not e.kubun and e.heading != DPC_HEADING)
    dpc = Counter(e.topic for e in entries if e.heading == DPC_HEADING)
    print("DPC headings (questions, chars):", [(t, n, sum(len("".join(e.q + e.a)) for e in entries if e.heading == DPC_HEADING and e.topic == t)) for t, n in dpc.items()], file=sys.stderr)
    if args.stats:
        sizes = []
        for k, n in by_kubun.most_common():
            chars = sum(len("".join(e.q + e.a)) for e in entries if e.kubun == k)
            sizes.append((chars, k, n))
        sizes.sort(reverse=True)
        print(f"kubun nodes {len(by_kubun)} / heading nodes {len(topics)}", file=sys.stderr)
        print("largest kubun:", sizes[:12], file=sys.stderr)
        print("kubun per part:", dict(sorted(Counter(k[0] for k in by_kubun).items())), file=sys.stderr)
        print("heading nodes (questions):", topics.most_common(60), file=sys.stderr)
        qlens = sorted(len("".join(e.q + e.a)) for e in entries)
        print(f"chars per question: median {qlens[len(qlens)//2]} / max {qlens[-1]} / total {sum(qlens)}", file=sys.stderr)
        return

    stats = write_tree(manifest, entries, corrections, abolished, excluded, tmap, how)
    print(
        f"疑義解釈R08: entries={len(entries)} kubun_nodes={len(by_kubun)} topic_nodes={len(topics)} dpc_nodes={len(dpc)} "
        f"nodes={stats['nodes']} split={stats['split']} max_body={stats['max_body']} "
        f"corrected={sum(len(c.entries) for c in corrections)} abolished={len(abolished)} "
        f"dirs={dict(stats['dirs'])}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
