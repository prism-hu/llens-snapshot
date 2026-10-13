"""HTML → structured intermediate JSON per CQ.

Reads `cache/html/2025/<page ID>.html` and, for each CQ, extracts:
- question and footnotes
- recommendations (a/b/c/d, strength, evidence level, agreement rate, 〈…の場合〉 group headings)
- explanation (per recommendation, body converted to Markdown, citations renumbered 1..k per CQ)
- references (only those cited in that CQ, after renumbering)

and writes them to <out>/<domain>/CQ{N}.json.
In addition, the **end-of-section regimen** (項末のレジメン, under `<dl class="title_3" id="rjmn">`)
of each page is collected into <out>/<domain>/REGIMEN.json (for inclusion in the chapter `_index.md`).
`render_lung_cancer_entries.py` reads this JSON and generates the node MD files.

    uv run python scripts/parse_to_entries.py [--out DIR] [--only DOMAIN]

`domain` equals the delivery directory name (e.g. `肺癌2025_診断`).
"""

from __future__ import annotations

import argparse
import json
import re
import unicodedata
import warnings
from pathlib import Path

from bs4 import BeautifulSoup, Comment, NavigableString, Tag, XMLParsedAsHTMLWarning

# Some pages start with an XML declaration but are HTML. Reading them with lxml's HTML parser is correct.
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

ROOT = Path(__file__).resolve().parent.parent
HTML_DIR = ROOT / "cache" / "html" / "2025"
OUT_DIR = ROOT / "cache" / "extracts"
SRC_URL_PREFIX = "https://www.haigan.gr.jp/publication/guideline/examination/2025"

# Target CQ list per page and the output domain (= delivery directory name).
# CQ numbers restart in each chapter, so the domain is explicit and JSON is split into <out>/<domain>/.
PAGES: list[dict] = [
    # Part 1 Lung cancer 2025, Chapter 1 Diagnosis (6 pages, CQ1-CQ28)
    {"path": "1/1/250101010100.html", "domain": "肺癌2025_診断", "chapter": "診断", "cq_ids": [str(i) for i in range(1, 5)]},
    {"path": "1/1/250101020100.html", "domain": "肺癌2025_診断", "chapter": "診断", "cq_ids": ["5", "6"]},
    {"path": "1/1/250101030100.html", "domain": "肺癌2025_診断", "chapter": "診断", "cq_ids": [str(i) for i in range(7, 16)]},
    {"path": "1/1/250101040100.html", "domain": "肺癌2025_診断", "chapter": "診断", "cq_ids": [str(i) for i in range(16, 23)]},
    {"path": "1/1/250101050100.html", "domain": "肺癌2025_診断", "chapter": "診断", "cq_ids": ["23", "24", "25"]},
    {"path": "1/1/250101060100.html", "domain": "肺癌2025_診断", "chapter": "診断", "cq_ids": ["26", "27", "28"]},
    # Part 1 Lung cancer 2025, Chapter 2 NSCLC (7 pages, CQ1-CQ70 + split CQ22-1/22-2)
    {"path": "1/2/250102010100.html", "domain": "肺癌2025_非小細胞癌", "chapter": "非小細胞癌", "cq_ids": [str(i) for i in range(1, 21)]},
    {"path": "1/2/250102020100.html", "domain": "肺癌2025_非小細胞癌", "chapter": "非小細胞癌", "cq_ids": ["21"]},
    {"path": "1/2/250102030100.html", "domain": "肺癌2025_非小細胞癌", "chapter": "非小細胞癌", "cq_ids": ["22-1", "22-2", "23"]},
    {"path": "1/2/250102040100.html", "domain": "肺癌2025_非小細胞癌", "chapter": "非小細胞癌", "cq_ids": [str(i) for i in range(24, 33)]},
    {"path": "1/2/250102050100.html", "domain": "肺癌2025_非小細胞癌", "chapter": "非小細胞癌", "cq_ids": [str(i) for i in range(33, 37)]},
    {"path": "1/2/250102060100.html", "domain": "肺癌2025_非小細胞癌", "chapter": "非小細胞癌", "cq_ids": [str(i) for i in range(37, 47)]},
    {"path": "1/2/250102070100.html", "domain": "肺癌2025_非小細胞癌", "chapter": "非小細胞癌", "cq_ids": [str(i) for i in range(47, 71)]},
    # Part 1 Lung cancer 2025, Chapter 3 SCLC (4 pages, CQ1-CQ19)
    {"path": "1/3/250103010100.html", "domain": "肺癌2025_小細胞癌", "chapter": "小細胞癌", "cq_ids": [str(i) for i in range(1, 10)]},
    {"path": "1/3/250103020100.html", "domain": "肺癌2025_小細胞癌", "chapter": "小細胞癌", "cq_ids": [str(i) for i in range(10, 14)]},
    {"path": "1/3/250103030100.html", "domain": "肺癌2025_小細胞癌", "chapter": "小細胞癌", "cq_ids": ["14", "15", "16"]},
    {"path": "1/3/250103040100.html", "domain": "肺癌2025_小細胞癌", "chapter": "小細胞癌", "cq_ids": ["17", "18", "19"]},
    # Part 1 Lung cancer 2025, Chapter 4 Metastasis etc. (6 pages, CQ1-CQ16)
    {"path": "1/4/250104010100.html", "domain": "肺癌2025_転移", "chapter": "転移など", "cq_ids": [str(i) for i in range(1, 6)]},
    {"path": "1/4/250104020100.html", "domain": "肺癌2025_転移", "chapter": "転移など", "cq_ids": [str(i) for i in range(6, 13)]},
    {"path": "1/4/250104030100.html", "domain": "肺癌2025_転移", "chapter": "転移など", "cq_ids": ["13"]},
    {"path": "1/4/250104040100.html", "domain": "肺癌2025_転移", "chapter": "転移など", "cq_ids": ["14"]},
    {"path": "1/4/250104050100.html", "domain": "肺癌2025_転移", "chapter": "転移など", "cq_ids": ["15"]},
    {"path": "1/4/250104060100.html", "domain": "肺癌2025_転移", "chapter": "転移など", "cq_ids": ["16"]},
    # Part 1 Lung cancer 2025, Chapter 5 Palliative care (1 page, CQ1-CQ3)
    {"path": "1/5/250105010100.html", "domain": "肺癌2025_緩和ケア", "chapter": "緩和ケア", "cq_ids": ["1", "2", "3"]},
    # Part 2 Mesothelioma 2025, Diagnosis (4 pages, CQ1-CQ14)
    {"path": "2/1/250201010100.html", "domain": "中皮腫2025_診断", "chapter": "診断", "cq_ids": ["1", "2"]},
    {"path": "2/1/250201020100.html", "domain": "中皮腫2025_診断", "chapter": "診断", "cq_ids": ["3", "4", "5"]},
    {"path": "2/1/250201030100.html", "domain": "中皮腫2025_診断", "chapter": "診断", "cq_ids": [str(i) for i in range(6, 14)]},
    {"path": "2/1/250201040100.html", "domain": "中皮腫2025_診断", "chapter": "診断", "cq_ids": ["14"]},
    # Part 2 Mesothelioma 2025, Treatment (4 pages, CQ1-CQ16)
    {"path": "2/2/250202010100.html", "domain": "中皮腫2025_治療", "chapter": "治療", "cq_ids": [str(i) for i in range(1, 6)]},
    {"path": "2/2/250202020100.html", "domain": "中皮腫2025_治療", "chapter": "治療", "cq_ids": ["6", "7", "8"]},
    {"path": "2/2/250202030100.html", "domain": "中皮腫2025_治療", "chapter": "治療", "cq_ids": [str(i) for i in range(9, 16)]},
    {"path": "2/2/250202040100.html", "domain": "中皮腫2025_治療", "chapter": "治療", "cq_ids": ["16"]},
    # Part 3 Thymic tumors 2025, Diagnosis (4 pages, CQ1-CQ8)
    {"path": "3/1/250301010100.html", "domain": "胸腺腫瘍2025_診断", "chapter": "診断", "cq_ids": ["1", "2", "3"]},
    {"path": "3/1/250301020100.html", "domain": "胸腺腫瘍2025_診断", "chapter": "診断", "cq_ids": ["4", "5", "6"]},
    {"path": "3/1/250301030100.html", "domain": "胸腺腫瘍2025_診断", "chapter": "診断", "cq_ids": ["7"]},
    {"path": "3/1/250301040100.html", "domain": "胸腺腫瘍2025_診断", "chapter": "診断", "cq_ids": ["8"]},
    # Part 3 Thymic tumors 2025, Treatment (6 pages, CQ1-CQ27)
    {"path": "3/2/250302010100.html", "domain": "胸腺腫瘍2025_治療", "chapter": "治療", "cq_ids": [str(i) for i in range(1, 11)]},
    {"path": "3/2/250302020100.html", "domain": "胸腺腫瘍2025_治療", "chapter": "治療", "cq_ids": [str(i) for i in range(11, 15)]},
    {"path": "3/2/250302030100.html", "domain": "胸腺腫瘍2025_治療", "chapter": "治療", "cq_ids": [str(i) for i in range(15, 24)]},
    {"path": "3/2/250302040100.html", "domain": "胸腺腫瘍2025_治療", "chapter": "治療", "cq_ids": ["24"]},
    {"path": "3/2/250302050100.html", "domain": "胸腺腫瘍2025_治療", "chapter": "治療", "cq_ids": ["25", "26"]},
    {"path": "3/2/250302060100.html", "domain": "胸腺腫瘍2025_治療", "chapter": "治療", "cq_ids": ["27"]},
]

_TITLE_LEVELS = ("title_5", "title_4", "title_3")


def find_subsection_title(cq_div: Tag) -> str:
    """Closest preceding sibling among title_3 / title_4 / title_5.

    EGFR/ALK have a subsection in title_5, but ROS1-HER2 only have title_4, and poor PS /
    general second-line hang directly under title_3. Use the closest title_X.
    Chapters with no title_X (diagnosis / SCLC LD / metastasis / palliative) fall back to the page header <h2>.
    """
    prev = cq_div
    while True:
        prev = prev.find_previous_sibling()
        if prev is None:
            break
        if not isinstance(prev, Tag):
            continue
        cls = prev.get("class") or []
        if any(level in cls for level in _TITLE_LEVELS):
            text = prev.get_text(separator="", strip=True)
            # title_3/4 carry a number prefix like "7-1-3．ROS1融合遺伝子陽性".
            # Subagents use subsection_title as is, so keep the original text here.
            return text
    # Fallback: the page <h2> (page titles such as `検出方法`, `骨転移`)
    h2 = cq_div.find_previous("h2")
    if h2:
        return h2.get_text(strip=True)
    return ""


def get_question(cq_div: Tag) -> tuple[str, bool]:
    """Question text from <div class="cq__title">.
    Returns (question, has_footnote_marker)."""
    title = cq_div.find("div", class_="cq__title")
    if not title:
        return "", False
    ps = title.find_all("p")
    if len(ps) < 2:
        p = ps[0] if ps else None
    else:
        p = ps[1]
    if p is None:
        return "", False
    has_footnote = bool(p.find("sup"))
    # Drop footnote markers (＊ ＊＊ ※). Some pages use <sup>, others a superscript-like
    # <span style>, so remove every element whose content is only a marker.
    for sup in p.find_all("sup"):
        sup.decompose()
    for el in p.find_all(True):
        text = el.get_text(strip=True)
        if text and set(text) <= set("＊*※"):
            has_footnote = True
            el.decompose()
    return p.get_text(strip=True), has_footnote


_NOTE_MARKER_RE = re.compile(r"^[＊*※注]+[：:]?\s*")
_VOTE_SENTENCE_RE = re.compile(r"下記に，[^。]*投票結果を記載する。?")


def _clean_inline(text: str) -> str:
    """Drop stray whitespace that crept inside parentheses and corner brackets.

    The source has links inside notes, so `get_text` produces spaces like `（ CQ63～66 ）`.
    """
    # Full-width spaces are the source typesetting; leave them. Only drop ASCII spaces from HTML newlines/indent.
    text = re.sub(r"[ \t\r\n]+", " ", text)
    text = re.sub(r"([（(「『])[ \t]+", r"\1", text)
    text = re.sub(r"[ \t]+([）)」』，。])", r"\1", text)
    # Also drop the space between a closing bracket and following Japanese (`」 を参照` → `」を参照`)
    text = re.sub(r"([）)」』])[ \t]+(?=[^ \t\u3000A-Za-z0-9(（])", r"\1", text)
    return text.strip()


def strip_note_marker(text: str) -> str:
    """Drop the leading ＊ / ＊＊ / ※ / 注: of a note body (the "marker removal" of the procedure doc)."""
    return _NOTE_MARKER_RE.sub("", text).strip()


def get_cq_footnote(cq_div: Tag) -> tuple[str | None, list[str]]:
    """Returns (notes joined verbatim, list of notes with markers removed).

    One CQ may have several notes (e.g. ＊ and ※ side by side). Besides the joined string,
    also return a list so they can become separate `注:` lines as the procedure doc says.
    """
    fn = cq_div.find("div", class_="cq__footnote")
    if not fn:
        return None, []
    parts = [_clean_inline(p.get_text(separator="", strip=True)) for p in fn.find_all("p")]
    parts = [p for p in parts if p]
    if not parts:
        return None, []
    return "\n".join(parts), [strip_note_marker(p) for p in parts]


_RECOMMEND_RE = re.compile(
    r"推奨の強さ[：:]\s*(?P<strength>\S+?)[，,]\s*エビデンスの強さ[：:]\s*(?P<evidence>\S+?)\s*〕"
)


def extract_recommendations(cq_div: Tag) -> tuple[list[dict], list[str]]:
    """Parse <dl class="cq__recommend"> into ([{label, text, strength, evidence}], [notes]).

    Recommendation `<dd>` elements include empty ones (NSCLC CQ62) and ones holding only
    a ＊ note instead of a recommendation (thymic tumors, diagnosis CQ8). Neither counts as a recommendation.
    """
    dl = cq_div.find("dl", class_="cq__recommend")
    recs: list[dict] = []
    dd_notes: list[str] = []
    if not dl:
        return recs, dd_notes
    for dd in dl.find_all("dd", recursive=False):
        rec = {"label": None, "group_heading": None, "text": "", "strength": "", "evidence": ""}
        # Group heading like 〈非小細胞肺癌の場合〉. Subsequent recommendations belong under it.
        midashi = dd.find("div", class_="cq__recommend_midashi")
        if midashi:
            rec["group_heading"] = midashi.get_text(strip=True)
            midashi.extract()
        ul = dd.find("ul", recursive=False)
        if ul:
            lis = ul.find_all("li", recursive=False)
            if len(lis) >= 2:
                label_text = lis[0].get_text(strip=True).rstrip("．").rstrip(".").strip()
                rec["label"] = label_text
                rec["text"] = lis[1].get_text(separator="", strip=True)
            elif len(lis) == 1:
                rec["text"] = lis[0].get_text(separator="", strip=True)
        else:
            # No <ul>: dd directly contains text + <p>〔...〕</p>
            text_parts: list[str] = []
            for c in dd.children:
                if isinstance(c, NavigableString):
                    s = str(c).strip()
                    if s:
                        text_parts.append(s)
                elif isinstance(c, Tag) and c.name != "p":
                    text_parts.append(c.get_text(strip=True))
            rec["text"] = "".join(text_parts).strip()
        for p in dd.find_all("p"):
            ptext = p.get_text(strip=True)
            m = _RECOMMEND_RE.search(ptext)
            if m:
                rec["strength"] = m.group("strength")
                rec["evidence"] = m.group("evidence")
                break
            if "推奨に至る根拠が明確ではない" in ptext:
                rec["strength"] = "明確ではない"
                rec["evidence"] = None
                break
        if not rec["strength"]:
            if not rec["text"]:
                continue  # empty <dd> (NSCLC CQ62)
            # A <dd> without a strength is a note, not a recommendation (the two ＊ in thymic tumors, diagnosis CQ8)
            for line in re.split(r"(?<=。)(?=[＊*※])", rec["text"]):
                line = strip_note_marker(line)
                if line:
                    dd_notes.append(line)
            continue
        recs.append(rec)
    return recs, dd_notes


def _body_paragraphs(div: Tag) -> list[Tag]:
    """Meaningful body <p> elements that are not inside a table."""
    return [
        p
        for p in div.find_all("p")
        if p.get_text(strip=True) and p.find_parent("table") is None
    ]


def collect_body_blocks(cq_div: Tag) -> list[dict]:
    """Siblings after cq_div until the next CQ / references / another subsection / another section.

    Returns list of {'kind': 'explanation' | 'vote' | 'note', 'tag': Tag}.

    The source is inconsistent in its classes: pages where the vote table sits in `text-layout_2`
    (SCLC, relapse), pages where the explanation continues in `text-layout_3`, and pages where the
    explanation sits in a div with no class (metastasis CQ12). Decide by **content**, not class.
    """
    blocks: list[dict] = []
    sib = cq_div
    while True:
        sib = sib.next_sibling
        if sib is None:
            break
        if not isinstance(sib, Tag):
            continue
        cls = sib.get("class") or []
        idv = sib.get("id") or ""
        if "cq" in cls and idv.startswith("cq"):
            break
        if "midashi_2" in cls:
            break
        if "title_5" in cls or "title_4" in cls or "title_3" in cls:
            break
        if "midashi_1" in cls:
            continue  # the 解 説 (explanation) marker, ignore
        if sib.name == "table":
            # The source sometimes places body tables (表1 etc.) as bare <table> without a div
            # (the multi-gene test table in diagnosis CQ16, the positive marker table in mesothelioma diagnosis CQ7).
            blocks.append({"kind": "vote" if "vote" in cls else "explanation", "tag": sib})
            continue
        if "title_2" in cls:
            # Table caption div (`表1　…`). Emit it in the body next to the following table
            blocks.append({"kind": "explanation", "tag": sib})
            continue
        if sib.find("table", class_="vote"):
            blocks.append({"kind": "vote", "tag": sib})
            continue
        paragraphs = _body_paragraphs(sib)
        if not paragraphs:
            if sib.find("table"):
                blocks.append({"kind": "explanation", "tag": sib})
            continue  # figure-only blocks are not picked up as body
        if all(_NOTE_MARKER_RE.match(p.get_text(strip=True)) for p in paragraphs):
            blocks.append({"kind": "note", "tag": sib})
        else:
            blocks.append({"kind": "explanation", "tag": sib})
    return blocks


def get_explanation_label(text_layout_2: Tag) -> str | None:
    """Which recommendation an explanation block corresponds to.

    The source may use `<span class="bold">a．</span>`, `<p class="bold">a．</p>`, or
    just start with a bare `a．`. Pick up any of them.
    """
    span = text_layout_2.find("span", class_="bold")
    if span:
        m = re.match(r"^([a-z])[．.]", span.get_text(strip=True))
        if m:
            return m.group(1)
    for p in _body_paragraphs(text_layout_2):
        m = re.match(r"^[　\s]*([a-z])[．.]", p.get_text(strip=True))
        if m:
            return m.group(1)
        break
    return None


_SUPERSCRIPT = str.maketrans("0123456789+-=()", "\u2070\u00b9\u00b2\u00b3\u2074\u2075\u2076\u2077\u2078\u2079\u207a\u207b\u207c\u207d\u207e")
# Range citations like `5）～7）` / `1～4）`. The href points only to the first one, so
# read the end from the anchor text and fill in the gap (otherwise [6][7] vanish from the body).
_RANGE_RE = re.compile(r"^\s*(\d+)\s*[）)]?\s*[～〜~-]\s*(\d+)\s*[）)]?\s*$")
_KEY_RE = re.compile(r"^(.*?)(\d+)$")


_SUP_WORDS = {"R": "\u00ae", "TM": "\u2122"}


def _ref_key_from_href(href: str | None) -> str | None:
    """`#b_7-1-1_5` / `#b116` (anchor typo in the source) → reference key. None if not a reference."""
    h = (href or "").lstrip("#")
    if h.startswith("b_"):
        return h.removeprefix("b_")
    if re.fullmatch(r"b\d+", h):
        return h[1:]
    return None


def to_superscript(text: str) -> str:
    """Map superscript digits/signs to superscript characters (mm2 → mm², R → ®). Everything else verbatim."""
    if text in _SUP_WORDS:
        return _SUP_WORDS[text]
    if text and all(c in "0123456789+-=()" for c in text):
        return text.translate(_SUPERSCRIPT)
    return text


def expand_citation_keys(first_key: str, anchor_text: str) -> list[str]:
    """`b_7-1-3_5` + `5）～7）` → ['7-1-3_5', '7-1-3_6', '7-1-3_7'].

    Returns [first_key] if not a range. Only the trailing number is swapped, so this works
    for both flat IDs and section-aware IDs even when numbering has gaps (`b_29` etc.).
    """
    m = _RANGE_RE.match(anchor_text or "")
    if not m:
        return [first_key]
    lo, hi = int(m.group(1)), int(m.group(2))
    km = _KEY_RE.match(first_key)
    if not km or int(km.group(2)) != lo or hi <= lo or hi - lo > 20:
        return [first_key]
    prefix = km.group(1)
    return [f"{prefix}{n}" for n in range(lo, hi + 1)]


def render_paragraph(p: Tag, citation_map: dict[str, int]) -> str:
    """<p> → markdown text. sup citations → [N], em → *X*, span.bold → **X**."""

    def walk(node) -> str:
        if isinstance(node, Comment):
            return ""  # the source embeds editorial comments (///注意/// forced line breaks)
        if isinstance(node, NavigableString):
            return str(node)
        if not isinstance(node, Tag):
            return ""
        if node.name == "sup":
            anchors = node.find_all("a")
            if anchors:
                out = []
                seen_href: set[str] = set()
                for a in anchors:
                    key0 = _ref_key_from_href(a.get("href"))
                    if key0 is None:
                        # Non-reference link (`（注釈）` in thymic tumors, treatment CQ10).
                        # It marks "the annotation applies here" in the body, so keep the text.
                        label = a.get_text(strip=True)
                        if label:
                            out.append(label)
                        continue
                    href = key0
                    if href in seen_href:
                        continue  # nested <a> in the source (`<a href="#b_8"><a href="#b_8">8）</a>`)
                    seen_href.add(href)
                    keys = expand_citation_keys(key0, a.get_text(strip=True))
                    nums = [citation_map.get(k) for k in keys]
                    marks = [f"[{n}]" if n else f"[ref:{k}]" for k, n in zip(keys, nums)]
                    # `5）～7）` in the source is a range citation. Collapse to [5]～[7] only when
                    # the renumbered values are consecutive (otherwise list them individually to avoid misreading).
                    contiguous = all(nums) and len(nums) > 2 and nums[-1] - nums[0] == len(nums) - 1
                    out.append(f"{marks[0]}～{marks[-1]}" if contiguous else "".join(marks))
                if out:
                    return "".join(out)
            # Non-reference superscript (the 2 of mm2, the 9 of 10e9, Ⓡ etc.). Digits map to superscript characters.
            return to_superscript(node.get_text(strip=True))
        if node.name == "em":
            return f"*{node.get_text(strip=True)}*"
        if node.name == "br":
            return ""  # <br> inside a paragraph is a typesetting line break; join as body text
        if node.name == "span":
            cls = node.get("class") or []
            if "update" in cls:
                return ""  # `修正 #N` link to the errata PDF; not body text
            inner = "".join(walk(c) for c in node.children)
            if "bold" in cls:
                return f"**{inner.strip()}**"
            return inner
        # a, etc → just inner content
        return "".join(walk(c) for c in node.children)

    return "".join(walk(c) for c in p.children)


def _has_header_row(table: Tag) -> bool:
    """Whether the first row is a column header.

    Most regimen tables start with a **row** label cell like `<th rowspan="2">CDDP＋ETP療法</th>`
    and have no column header row. Markdown tables eat the first row as a header, so such
    tables get an empty header row added to keep every row as data.
    """
    groups = _cell_groups(table)
    if not groups:
        return False
    first = groups[0]
    return all(c.name == "th" for c in first) and all(
        int(c.get("rowspan", 1) or 1) == 1 for c in first
    )


def render_table(table: Tag, citation_map: dict[str, int], implicit_header: bool = False) -> str:
    """Source table → markdown table. Used to keep non-vote tables (表1-表4 etc.) in the body.

    With `implicit_header`, tables lacking a column header row get an empty header row.
    """
    rows: list[list[str]] = []
    pending: dict[int, tuple[int, str]] = {}  # col -> (rows left, value)  rowspan carry-over
    # Do not rely on `<tr>`. The source has tables where cells sit bare right after `<tr class="border"></tr>`
    # (the "ベバシズマブ併用" row of the stage IV regimen), which `find_all("tr")` would drop entirely.
    for cells in _cell_groups(table):
        row: list[str] = []
        col = 0
        it = iter(cells)
        while True:
            while col in pending:
                left, val = pending[col]
                row.append(val)
                if left <= 1:
                    del pending[col]
                else:
                    pending[col] = (left - 1, val)
                col += 1
            c = next(it, None)
            if c is None:
                break
            text = render_paragraph(c, citation_map)
            # Collapse newlines/tabs/runs of spaces inside a cell (full-width spaces are source typesetting; keep).
            # `|` would become a markdown column separator, so escape it.
            text = re.sub(r"[ \t\r\n]+", " ", text).strip().replace("|", "\\|") or " "
            span = max(1, int(c.get("colspan", 1) or 1))
            rspan = max(1, int(c.get("rowspan", 1) or 1))
            for k in range(span):
                row.append(text if k == 0 else " ")
                if rspan > 1:
                    pending[col] = (rspan - 1, text if k == 0 else " ")
                col += 1
        rows.append(row)
    # Malformed tables without <tr> (present in the source): wrap cells at the first row's column count
    if not rows:
        cells = table.find_all(["th", "td"])
        if not cells:
            return ""
        rows = [[render_paragraph(c, citation_map).strip() or " " for c in cells]]
    width = max(len(r) for r in rows)
    rows = [r + [" "] * (width - len(r)) for r in rows]
    if implicit_header and not _has_header_row(table):
        rows.insert(0, [" "] * width)
    out = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
    for r in rows[1:]:
        out.append("| " + " | ".join(r) + " |")
    return "\n".join(out)


def render_explanation(text_layout_2: Tag, citation_map: dict[str, int]) -> str:
    """text-layout_2 div → markdown body (paragraphs separated by blank lines)."""
    md_paragraphs: list[str] = []
    # The block itself may be a table / caption div (the bare <table> mentioned above)
    for node in [text_layout_2] + text_layout_2.find_all(["p", "table", "div"]):
        if node.name == "div":
            if "title_2" not in (node.get("class") or []):
                continue
            cap = _clean_inline(node.get_text(separator="", strip=False))
            if cap:
                md_paragraphs.append(f"**{cap}**")
            continue
        if node.name == "table":
            if "vote" in (node.get("class") or []):
                continue
            if node.find_parent("table") is not text_layout_2.find_parent("table"):
                continue
            md = render_table(node, citation_map)
            if md:
                md_paragraphs.append(md)
            continue
        p = node
        if p.find_parent("table"):
            continue  # table contents are handled by render_table
        md = render_paragraph(p, citation_map)
        # Drop leading full-width / half-width indent space the source uses
        md = md.lstrip("　 ")
        md = md.strip()
        if not md:
            continue
        if "bold" in (p.get("class") or []) and not md.startswith("**"):
            md = f"**{md}**"
        md_paragraphs.append(md)
    return "\n\n".join(md_paragraphs)


def extract_regimen(soup: BeautifulSoup, source_base: str) -> dict | None:
    """Convert the end-of-page "regimen" (end-of-section regimen) to markdown.

    The source puts `<dl class="title_3" id="rjmn"><dt>レジメン</dt><dd>section name</dd></dl>` at the
    end of each section, followed by `midashi_4` / `midashi_5` subheadings and dose/cycle tables.
    CQ explanations only point here ("see the end-of-section regimen"), so without picking it up
    the referenced target would be missing from the delivery tree.

    Returns {"heading": section name, "source": URL#rjmn, "markdown": body}. None if absent.
    """
    dl = soup.find("dl", class_="title_3", id="rjmn")
    if dl is None:
        return None
    dd = dl.find("dd")
    heading = dd.get_text(strip=True) if dd else "レジメン"

    parts: list[str] = []

    def walk(node: Tag) -> None:
        for child in node.children:
            if isinstance(child, NavigableString):
                text = _clean_inline(str(child))
                if text:
                    parts.append(text)
                continue
            if not isinstance(child, Tag):
                continue
            cls = child.get("class") or []
            if child.name == "table":
                # <br> inside regimen table cells separates distinct values like 「90 mg/日，day 1-7」
                # 「180 mg/日，day 8～」. Dropping it would join into `day 1-7180 mg/日`, so open it to a space.
                for br in child.find_all("br"):
                    br.replace_with(NavigableString(" "))
                md = render_table(child, {}, implicit_header=True)
                if md:
                    parts.append(md)
            elif child.name == "p":
                md = render_paragraph(child, {}).lstrip("　 ").strip()
                if md:
                    parts.append(md)
            elif child.name == "div" and ("midashi_4" in cls or "midashi_5" in cls):
                text = _clean_inline(child.get_text(separator="", strip=True))
                if text:
                    parts.append(("## " if "midashi_4" in cls else "### ") + text)
            elif child.name == "span" and "update" in cls:
                continue  # `修正 #N` link to the errata PDF
            elif child.name in ("div", "section"):
                walk(child)

    sib = dl
    while True:
        sib = sib.next_sibling
        if sib is None:
            break
        if not isinstance(sib, Tag):
            continue
        cls = sib.get("class") or []
        if sib.name == "dl" and "title_3" in cls:
            break
        if "midashi_2" in cls:
            break  # references
        if sib.name == "span":
            continue  # placeholder for the errata link (empty)
        if sib.name == "div" and ("midashi_4" in cls or "midashi_5" in cls):
            text = _clean_inline(sib.get_text(separator="", strip=True))
            if text:
                parts.append(("## " if "midashi_4" in cls else "### ") + text)
            continue
        walk(sib)

    markdown = "\n\n".join(parts).strip()
    if not markdown:
        return None
    return {"heading": heading, "source": f"{source_base}#rjmn", "markdown": markdown}


def find_cited_refs(blocks: list[dict]) -> list[str]:
    """Walk explanation blocks in order, collect unique ref keys (e.g. '7-1-1_1')."""
    seen: list[str] = []
    for b in blocks:
        if b["kind"] != "explanation":
            continue
        for sup in b["tag"].find_all("sup"):
            for a in sup.find_all("a"):
                key0 = _ref_key_from_href(a.get("href"))
                if key0 is None:
                    continue
                for key in expand_citation_keys(key0, a.get_text(strip=True)):
                    if key not in seen:
                        seen.append(key)
    return seen


_VOTE_HEADER_WORDS = ("行うことを", "行わないことを", "明確ではない")
# Column meaning: 0 strongly for / 1 weakly for / 2 not clear / 3 weakly against / 4 strongly against
_VOTE_COL = {
    ("1", "positive"): 0,
    ("2", "positive"): 1,
    ("明確ではない", "positive"): 2,
    ("明確ではない", "negative"): 2,
    ("2", "negative"): 3,
    ("1", "negative"): 4,
}


def _cell_groups(table: Tag) -> list[list[Tag]]:
    """Group table cells into "rows".

    In the source HTML some rows lack `<tr>` (row a of mesothelioma treatment CQ2), and some
    data rows use `<th>` instead of `<td>` (metastasis CQ15). Do not rely on `<tr>`; treat a
    run of cells sharing the same parent as one row.
    """
    groups: list[list[Tag]] = []
    current_parent = None
    for cell in table.find_all(["td", "th"]):
        if cell.find_parent("table") is not table:
            continue  # cell of a nested table (picked up when rendering that table)
        parent = cell.parent
        if parent is not current_parent or not groups:
            groups.append([])
            current_parent = parent
        groups[-1].append(cell)
    return [g for g in groups if g]


def _is_header_group(group: list[Tag]) -> bool:
    text = "".join(c.get_text(strip=True) for c in group)
    return any(w in text for w in _VOTE_HEADER_WORDS)


def vote_year(table: Tag) -> str | None:
    """Read `実施年度：2025年` from the vote table caption.

    The source caption is `投票者の所属委員会：…/実施年度：2025年`. The vote year can differ per
    recommendation within one CQ (NSCLC CQ49: a/b in 2025, c in 2018), so read it per table
    and copy it to the recommendation row. Re-vote `［2回目］` tables have no year in the caption,
    so the caller inherits the year of the previous table in the same div.
    """
    cap = table.find("caption")
    if not cap:
        return None
    m = re.search(r"実施年度\s*[：:]\s*(\d{4})\s*年", unicodedata.normalize("NFKC", cap.get_text(" ")).replace(":", "："))
    return f"{m.group(1)}年" if m else None


def vote_round(table: Tag) -> int | None:
    """Read `［2回目］` from the vote table caption.

    For a CQ that was re-voted after failing to reach agreement (diagnosis CQ5, recommendation b),
    the first and second round tables for the same recommendation sit **in the same div**.
    The final round is the agreement rate, so earlier rounds are discarded.
    """
    cap = table.find("caption")
    if not cap:
        return None
    m = re.search(r"[［\[]\s*(\d+)\s*回目\s*[］\]]", unicodedata.normalize("NFKC", cap.get_text(" ")))
    return int(m.group(1)) if m else None


def extract_vote_rows(vote_div: Tag) -> list[dict]:
    """Extract rows from one vote table div.

    Returns a list of {label, cells, note}. `label` is "a"/"b"/… when the table has a leading
    recommendation label column (`<th class="vote__taxonomy...">`), else None.
    One table may hold several recommendations (a and b) one row each, so return per row.
    """
    rows: list[dict] = []
    note = None
    for p in _body_paragraphs(vote_div):
        text = p.get_text(separator=" ", strip=True)
        if "indent-2em" in (p.get("class") or []) or text.startswith("注"):
            note = text
            break
    last_year = None
    for table in vote_div.find_all("table", class_="vote"):
        groups = _cell_groups(table)
        header = next((g for g in groups if _is_header_group(g)), None)
        labeled = bool(header) and len(header) == 6
        width = 6 if labeled else 5
        year = vote_year(table) or last_year
        last_year = year
        this: list[dict] = []
        for g in groups:
            if g is header or _is_header_group(g):
                continue
            if len(g) != width:
                continue
            label = g[0].get_text(strip=True) if labeled else None
            this.append({"label": label or None, "cells": g[-5:], "note": note, "year": year})
        if vote_round(table) not in (None, 1) and rows:
            # Re-vote of the same recommendation. Drop the previous round's rows and use the final round
            del rows[-len(this):]
        rows.extend(this)
    return rows


def vote_info_from_row(row: dict, target_strength: str, direction: str) -> dict:
    """Row + recommendation direction → {rate, total, note}."""
    info: dict = {"rate": None, "total": None, "note": row.get("note"), "year": row.get("year")}
    idx = _VOTE_COL.get((target_strength, direction))
    if idx is None:
        return info
    text = row["cells"][idx].get_text(separator="|", strip=True)
    parts = [p for p in text.split("|") if p]
    if parts:
        info["rate"] = parts[0]
        if len(parts) > 1:
            info["total"] = parts[1]
    return info


def get_recommendation_direction(rec_text: str) -> str:
    """Return 'negative' if the recommendation says '行わない', else 'positive'."""
    return "negative" if "行わない" in rec_text else "positive"


_HALFWIDTH_MAP = str.maketrans(
    "０１２３４５６７８９％（）／",
    "0123456789%()/",
)


def to_halfwidth_numbers(text: str | None) -> str | None:
    """Half-width only the digits/symbols of semi-structured fields like the agreement rate. Japanese is untouched."""
    if text is None:
        return None
    return text.translate(_HALFWIDTH_MAP)


def extract_shared_refs(soup: BeautifulSoup) -> dict[str, dict]:
    """Build dict of ref_key (e.g. '7-1-1_1') → {citation, url}."""
    refs: dict[str, dict] = {}
    for lit in soup.find_all("dl", class_="literature"):
        for dt in lit.find_all("dt"):
            key = _ref_key_from_href(dt.get("id", ""))
            if key is None:
                continue
            dd = dt.find_next_sibling("dd")
            if not dd:
                continue
            a = dd.find("a")
            url = a.get("href") if a else None
            citation = _clean_inline(dd.get_text(separator="", strip=False))
            if url and citation.endswith(url):
                citation = citation[: -len(url)].rstrip()
            refs[key] = {"citation": citation, "url": url}
    return refs


_REGIMEN_MENTION = re.compile(r"項末")


def parse_cq(
    soup: BeautifulSoup,
    cq_id_str: str,
    shared_refs: dict[str, dict],
    source_base: str,
    regimen: dict | None = None,
    domain: str | None = None,
) -> dict | None:
    """`cq_id_str` is the number as a str (split CQs like `22-1` are allowed)."""
    cq_id = f"cq{cq_id_str}"
    cq_div = soup.find("div", class_="cq", id=cq_id)
    if not cq_div:
        return None

    question, has_qfn = get_question(cq_div)
    cq_footnote, notes = get_cq_footnote(cq_div)
    recs, dd_notes = extract_recommendations(cq_div)
    notes = notes + dd_notes
    subsection_title = find_subsection_title(cq_div)
    blocks = collect_body_blocks(cq_div)
    expl_blocks = [b for b in blocks if b["kind"] == "explanation"]
    vote_blocks = [b for b in blocks if b["kind"] == "vote"]
    for blk in blocks:
        if blk["kind"] != "note":
            continue
        for np_ in _body_paragraphs(blk["tag"]):
            text = strip_note_marker(np_.get_text(separator=" ", strip=True))
            if text and text not in notes:
                notes.append(text)

    # Collect vote table rows across the whole CQ, then assign them to recommendations.
    # Tables with a label column match by label; those without match remaining recommendations in order.
    vote_rows: list[dict] = []
    for blk in vote_blocks:
        vote_rows.extend(extract_vote_rows(blk["tag"]))
    by_label = {}
    for row in vote_rows:
        if row["label"] and row["label"] not in by_label:
            by_label[row["label"]] = row
    unlabeled = [row for row in vote_rows if not row["label"]]
    next_unlabeled = 0
    for i, rec in enumerate(recs):
        rec["direction"] = get_recommendation_direction(rec["text"])
        row = None
        if rec["label"] and rec["label"] in by_label:
            row = by_label[rec["label"]]
        elif next_unlabeled < len(unlabeled):
            row = unlabeled[next_unlabeled]
            next_unlabeled += 1
        elif len(vote_rows) == 1 and len(recs) > 1:
            # A CQ with only one table voted on a and b together
            # (diagnosis CQ8 「下記に，上記a，bに関して…投票結果を記載する」)
            row = vote_rows[0]
        elif i < len(vote_rows):
            row = vote_rows[i]
        if row is None:
            rec["agreement_rate"] = None
            rec["agreement_total"] = None
            rec["agreement_note"] = None
            rec["agreement_year"] = None
            continue
        info = vote_info_from_row(row, rec["strength"], rec["direction"])
        rec["agreement_rate"] = to_halfwidth_numbers(info["rate"])
        rec["agreement_total"] = to_halfwidth_numbers(info["total"])
        rec["agreement_year"] = info["year"]
        note = info["note"]
        rec["agreement_note"] = _clean_inline(note) if note else None

    # 「下記に，…投票結果を記載する。」 may sit in an explanation paragraph or inside the vote
    # table div. Record its presence so the MD side can replace it with boilerplate either way.
    vote_sentence = False
    sentence_after: set[int] = set()  # the guide sentence followed this explanation block
    expl_seen = -1
    for blk in blocks:
        if blk["kind"] == "explanation":
            expl_seen += 1
        hit = any(
            _VOTE_SENTENCE_RE.search(p.get_text(separator="", strip=True))
            for p in _body_paragraphs(blk["tag"])
        )
        if hit:
            vote_sentence = True
            if blk["kind"] != "explanation" and expl_seen >= 0:
                sentence_after.add(expl_seen)

    # Renumber cited refs 1..k
    cited_keys = find_cited_refs(blocks)
    citation_map = {k: i + 1 for i, k in enumerate(cited_keys)}

    explanations = []
    for blk in expl_blocks:
        label = get_explanation_label(blk["tag"])
        md = render_explanation(blk["tag"], citation_map)
        # The leading `**a．**` / `a．` is replaced by the `### a. …` heading, so drop it
        # (not necessarily the first paragraph: some CQs have a preamble paragraph first)
        if label:
            # Keep a label that forms a paragraph on its own (`<p class="bold">a．</p>`).
            # Only drop labels attached to body text.
            md = re.sub(
                rf"(?m)^(?:\*\*{label}[．.]\*\*|{label}[．.])[　 ]*(?=\S)",
                "",
                md,
                count=1,
            )
        if not md.strip():
            continue  # skip empty blocks (text-layout_2 left with only whitespace in the source)
        explanations.append({"label": label, "markdown": md})

    # Empty blocks were skipped, so recount in the original order before marking
    for ex in explanations:
        ex["vote_sentence_after"] = False
    kept_idx = [
        i
        for i, blk in enumerate(b for b in blocks if b["kind"] == "explanation")
        if render_explanation(blk["tag"], citation_map).strip()
    ]
    for pos, orig in enumerate(kept_idx):
        if orig in sentence_after and pos < len(explanations):
            explanations[pos]["vote_sentence_after"] = True

    references = []
    for i, key in enumerate(cited_keys):
        ref = shared_refs.get(key)
        if not ref:
            references.append({"n": i + 1, "citation": f"[未解決: {key}]", "url": None})
            continue
        references.append({"n": i + 1, "citation": ref["citation"], "url": ref["url"]})

    # CQs that say "see the end-of-section regimen" get a way to reach that section's regimen
    # node (in the delivery tree the section end is a separate node, so it is otherwise unreachable).
    # path is the chapter directory identifier (no prefix). render_lung_cancer_entries.py converts it
    # to the canonical body form `{{DOC_BASE}}/…/` when writing out
    regimen_ref = None
    if regimen and domain and any(
        _REGIMEN_MENTION.search(ex["markdown"]) for ex in explanations
    ):
        regimen_ref = {
            "path": f"/診療ガイドライン/{domain}",
            "heading": regimen["heading"],
        }

    return {
        "id": f"CQ{cq_id_str}",
        "anchor": cq_id,
        "source": f"{source_base}#{cq_id}",
        "regimen_ref": regimen_ref,
        "subsection_title": subsection_title,
        "question": question,
        "question_has_footnote": has_qfn,
        "cq_footnote": cq_footnote,
        "notes": notes,
        "vote_sentence": vote_sentence,
        "recommendations": recs,
        "explanations": explanations,
        "references": references,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(OUT_DIR), help="output directory (default: cache/extracts)")
    ap.add_argument("--only", action="append", default=None, help="process only this domain (repeatable)")
    args = ap.parse_args()
    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)
    total = 0
    regimens: dict[str, list[dict]] = {}
    for page_cfg in PAGES:
        domain = page_cfg["domain"]
        if args.only and domain not in args.only:
            continue
        page_path = HTML_DIR / Path(page_cfg["path"]).name
        source = f"{SRC_URL_PREFIX}/{page_cfg['path']}"
        domain_dir = out_root / domain
        domain_dir.mkdir(parents=True, exist_ok=True)
        with open(page_path, encoding="utf-8") as f:
            soup = BeautifulSoup(f.read(), "lxml")
        shared_refs = extract_shared_refs(soup)
        regimen = extract_regimen(soup, source)
        if regimen:
            regimens.setdefault(domain, []).append(regimen)
        print(f"== [{domain}] {page_cfg['path']} ({len(shared_refs)} refs, target {len(page_cfg['cq_ids'])} CQs)"
              + (f" + regimen「{regimen['heading']}」" if regimen else ""))
        for cq_id_str in page_cfg["cq_ids"]:
            data = parse_cq(soup, cq_id_str, shared_refs, source, regimen, domain)
            if data is None:
                print(f"  WARN: CQ{cq_id_str} not found in {page_cfg['path']}")
                continue
            data["domain"] = domain
            data["chapter"] = page_cfg["chapter"]
            out_path = domain_dir / f"CQ{cq_id_str}.json"
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            total += 1
            n_recs = len(data["recommendations"])
            n_refs = len(data["references"])
            n_expl = len(data["explanations"])
            missing = sum(1 for r in data["recommendations"] if not r["agreement_rate"])
            flag = f"  !! {missing} rec without agreement rate" if missing else ""
            print(f"  CQ{cq_id_str}: {n_recs} rec / {n_expl} expl / {n_refs} refs{flag}")
    for domain, blocks in regimens.items():
        with open(out_root / domain / "REGIMEN.json", "w", encoding="utf-8") as f:
            json.dump(blocks, f, ensure_ascii=False, indent=2)
    print(f"== {total} CQ written to {out_root} "
          f"(regimen {sum(len(v) for v in regimens.values())} sections / {len(regimens)} domain)")


if __name__ == "__main__":
    main()
