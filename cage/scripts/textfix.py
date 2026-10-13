#!/usr/bin/env python3
"""Shared functions that repair pdftotext-derived text (imported by build_*.py).

Every build uses pdftotext output as the body verbatim, so **breakage originating in
the PDF text layer shows up in the same shape across corpora**. This module collects
those repairs in one place; no function here "deletes lines or characters", each only
"moves the text toward the glyphs visible on the original page".

  - `strip_invisible()` … drop characters invisible on the page (C0 controls, zero-width, soft hyphen)
  - `fix_radicals()`    … fold Kangxi radicals, CJK radical supplement and vertical forms to ordinary characters
  - `fix_symbol_bullets()` … restore bullet symbols that Wingdings dropped into the private use area
  - `despace_cjk()`     … remove spaces from lines where justified text became one ASCII space per character
                          (+ collapse the single stray space right after a line-initial circled number)
  - `promote_toc_headings()` … turn body lines that are section headings like 「Ⅱ．曝露部位の洗浄法」
                          into markdown headings, **only for lines listed in the document's own TOC**
  - `safe_segment()`    … make a node name (file/directory name under data/doc) URL-safe
                          (whitespace → `_`, ASCII that breaks URLs/markdown → full-width)

Usage (imports from sibling scripts follow the existing cage convention):

    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent))
    from textfix import despace_cjk, fix_symbol_bullets, strip_invisible

Tests live in `scripts/test_textfix.py` (`uv run python -m pytest scripts/test_textfix.py`).

`build_jns_guidelines.py` (= the generic build also shared by cancer registry / dermatology)
dropped its duplicate implementation on 2026-09-12 and imports from here. Changing the behavior
of `strip_invisible` / `fix_radicals` / `despace_cjk_text` affects all 3 corpora at once.
"""

from __future__ import annotations

import hashlib
import re
import sys
import unicodedata

# -----------------------------------------------------------------------------
# Invisible characters
# -----------------------------------------------------------------------------
# Invisible characters to drop: zero-width (U+00AD/200B/200C/200D/FEFF) and **C0 controls**
# (tab U+0009 and newline U+000A are kept). The 「①②③」 on medical safety manual p.1132-1140
# are followed by BEL (U+0007); unless dropped it lands in the body and grep stops matching.
# U+000C (form feed) passes through **after** page splitting, so dropping it here is fine.
#
# **U+FFFD (REPLACEMENT CHARACTER) is not included.** It marks "a glyph in the original could
# not be read"; dropping it would silently hide the gap (the one case in infection control
# manual 15-24 is a large ✗ mark in the original, already cropped as a figure). Keeping it is right.
_INVISIBLE = re.compile(r"[­​‌‍﻿\x00-\x08\x0b-\x1f\x7f]")


def strip_invisible(text: str) -> str:
    """Drop zero-width characters and C0 controls (except tab/newline). U+FFFD is kept."""
    return _INVISIBLE.sub("", text)


# -----------------------------------------------------------------------------
# Radical and vertical-form glyphs
# -----------------------------------------------------------------------------
# Glyphs from Kangxi radicals (`⾏` U+2F8F), CJK radical supplement (`⺠` U+2EA0) and vertical
# forms (`︓` U+FE13). The original text layer sometimes contains these forms (「⾎液」 in
# infection control manual 2-1, 「⽇本泌尿器科学会」 in 疑義解釈その2); `regex=True` grep then
# misses, and when the model quotes the body the glyph noise comes out verbatim.
_RADICAL_FORMS = re.compile(r"[⺀-⻳⼀-⿕︐-︙]")
# Some CJK radical supplement characters **have no NFKC mapping** (`⺠` U+2EA0 appeared in
# 「住⺠登録」, `⻑` U+2ED1 in 「⻑野」「⻑崎」, `⻘` U+2ED8 in 「⻘森」). NFKC alone leaves
# them, so add fixes **restricted to the ranges above**. Confirm a real usage in the original before adding
_RADICAL_EXTRA = {"⺠": "民", "⻄": "西", "⻑": "長", "⻘": "青", "⿊": "黒"}


def fix_radicals(text: str) -> str:
    """Fold **only** radical / vertical-form glyphs to ordinary characters, one at a time.

    NFKC over the whole text would also flatten full-width alphanumerics, ligatures and circled
    numbers, making it differ from the original, so only characters in the 3 ranges above are
    replaced (NFKC → `_RADICAL_EXTRA` for those with no mapping).
    """
    return _RADICAL_FORMS.sub(
        lambda mo: _RADICAL_EXTRA.get(mo.group(0)) or unicodedata.normalize("NFKC", mo.group(0)),
        text,
    )


# -----------------------------------------------------------------------------
# Bullet symbols from symbol fonts
# -----------------------------------------------------------------------------
# Wingdings places symbols at ASCII code positions; having no Unicode counterpart, pdftotext
# emits the private-use character `0xF000 + ASCII` as is. On the page they appear as the
# bullet symbols ● ■ □ ◆ ….
#
# The 11 characters in the table were verified by eye against the original page images for
# **all** 229 occurrences (210 medical safety + 19 infection control; review of 2026-09-12).
# All are line-initial bullets, checkboxes or heading markers; none intrudes into a word or number.
_SYMBOL_BULLETS = {
    "": "●",  # black circle
    "": "■",  # black square (heading marker)
    "": "□",  # white square (checkbox)
    "": "◆",  # black diamond
    "": "・",  # small dot
    "": "•",  # small middle dot
    "": "・",  # small dot/square
    "": "▪",  # small black square
    "": "✧",  # white four-pointed star
    "": "➢",  # arrow
    "": "✓",  # check mark
}
_PUA = re.compile(r"[-]")

_warned_pua: set[str] = set()


def _warn_unknown_pua(ch: str) -> None:
    """Warn once per distinct unknown private-use character (deduped within the process)."""
    if ch in _warned_pua:
        return
    _warned_pua.add(ch)
    print(
        f"  ! textfix: private-use character U+{ord(ch):04X} not in the mapping table was left in the body"
        " (check the glyph on the original page and add it to textfix._SYMBOL_BULLETS)",
        file=sys.stderr,
    )


def fix_symbol_bullets(text: str, warn=_warn_unknown_pua) -> str:
    """Restore Wingdings-derived private-use characters to the symbols visible in the original.

    **Private-use characters not in the table are kept with a warning** (not dropped, not guessed).
    Symbol fonts assign differently per PDF, so a blanket range mapping silently misconverts
    when another font is mixed in. If left in, the next mechanical scan surfaces them again.
    The build is not failed because some corpora handle SymbolMT PUA on the manifest side,
    and this must not collide with that.

    `warn` is a callable receiving one unknown character (replaceable for tests).
    """

    def repl(mo: re.Match) -> str:
        ch = mo.group(0)
        mapped = _SYMBOL_BULLETS.get(ch)
        if mapped is None:
            warn(ch)
            return ch
        return mapped

    return _PUA.sub(repl, text)


# -----------------------------------------------------------------------------
# Inter-character spaces (traces of justified text)
# -----------------------------------------------------------------------------
# A line the original set with full justification comes out of pdftotext as 「抑 制 具 の使 用」.
# The walker's `_normalize()` is only NFKC + lower and does not collapse spaces, so
# **`grep_doc("抑制具の使用")` returns 0 hits although the text is in the body**
# (measured on 1,920 lines across 49 medical safety nodes).
#
# Only kana, kanji, iteration marks such as 々〆ヶ, and the middle dot (U+30FB) count as CJK.
# Punctuation and brackets (most of U+3000-U+303F) and full-width alphanumerics are **excluded**,
# so legitimate gaps like the space after 「、 」 are untouched. The middle dot is included for
# headings like 「ク ロ ス ト リ ジ オ イ デ ス ・ デ ィ フ ィ シ ル」; without it a half-fixed line
# 「クロストリジオイデス ・ ディフィシル」 remains with gaps only around the dot, and grep still misses.
_CJK = r"々〆〻ぁ-ゖゝゞァ-ヿ㐀-䶿一-鿿豈-﫿"
# "CJK + one ASCII space + CJK". Both sides are CJK, so column alignment with 2+ spaces does not match
_SPACED_PAIR = re.compile(rf"(?<=[{_CJK}]) (?=[{_CJK}])")
# Adjacent CJK with no space between (ordinary unbroken body text)
_TIGHT_PAIR = re.compile(rf"(?<=[{_CJK}])(?=[{_CJK}])")

DESPACE_MIN_PAIRS = 3  # lines with fewer pairs are untouched
# Lower bound on the share of spaced pairs among all adjacent CJK pairs.
# **Lowered 0.5 → 0.4 (2026-09-12)**. Measurements on 3 corpora confirmed that 0.5 misses
# some lines while 0.4 still protects legitimate spaces:
#   - Lines newly touched at 0.40: neurology 11 / medical safety 196 / dermatology 2.
#     They are justified lines whose ratio was diluted by alphanumerics or already-tight words,
#     like 「２）周 囲 の皮 膚 に薬 液 が漏 れないように吸 引 しながら抜 針 する。」 (ratio 0.44).
#   - Lines that merely list words with single spaces (「糖尿病・内分泌内科 医師 中村 昭伸」 ratio 0.21,
#     committee rosters, table headers, reference author names) stay **at or below 0.375** in all 3 corpora.
#     In the 0.40-0.50 band only one line "contains no single-character token = word list shape",
#     and that one was in fact justified text (all checked).
#   - The only false join at 0.40 is the colophon 「発行所 株式会社 南 江 堂」 (2 lines) → 「発行所株式会社南江堂」.
#     Substring grep then matches both 「南江堂」 and 「株式会社南江堂」 (0 hits before), so search
#     improves. Going lower reaches legitimate spaces, so 0.40 is the floor.
DESPACE_MIN_RATIO = 0.4


# -----------------------------------------------------------------------------
# A single stray space right after a line-initial circled number
# -----------------------------------------------------------------------------
# The threshold above captures "the whole line is justified", so it cannot catch breakage
# with **only one space in the line**. 「麻酔器の始業点検」 on medical safety manual
# p.1136-1137/1139 has that shape: raw pdftotext output is `①<BEL>酸 素の補助ボンベを全開にし、`,
# the circled number drags a BEL (U+0007) and the character advance right after it is off by
# one, producing a single space (BEL is dropped by `strip_invisible`, so by the time despace
# sees it the line is `①酸 素の…`). The original page images (p.1136 「⑤低酸素防止装置付き流量計」,
# p.1137 「⑦空気流量計のノブを開き」, p.1139 「①ボタンやレバーの紛失・破損がないか。」) were
# checked by eye: **there is no space on the page**.
#
# So only the position-locked shape "**line-initial** circled number + 1 CJK + 1 ASCII space + CJK"
# is collapsed. Scanning every line of every corpus under data/doc (in-hospital manuals,
# clinical guidelines, medical fees, cancer registry) matched only the 16 lines above = 0 collateral.
# Word-list lines never take this position (a layout like 「① 酸素」 with a space right after the
# circled number has a non-CJK left side, so it does not even match `_SPACED_PAIR`).
_CIRCLED_NUM = r"①-⑳㉑-㉟㊱-㊿⓪⓫-⓴❶-❿"
_CIRCLED_STRAY_SPACE = re.compile(rf"^([{_CIRCLED_NUM}][{_CJK}]) (?=[{_CJK}])")


def despace_cjk(line: str) -> str:
    """Remove single ASCII spaces between CJK characters, **only** from lines spread by justification.

    Decided per line. Applied only when the number of "CJK␣CJK" in the line is at least
    `DESPACE_MIN_PAIRS` and they make up at least `DESPACE_MIN_RATIO` of all adjacent CJK
    pairs (spaced + unspaced). The thresholds keep legitimate spaces like 「A 型 ボツリヌス」
    「中村 昭伸」; spaces next to digits, Latin letters or symbols are never touched anyway
    (they are not between CJK).

    Table column alignment (2+ spaces) does not match either, since both sides must be CJK.

    Lines below the threshold get only the narrow position-locked rule `_CIRCLED_STRAY_SPACE`
    (the single stray space after a line-initial circled number). Results for lines the
    threshold handles are unchanged (those drop every space).
    """
    spaced = len(_SPACED_PAIR.findall(line))
    if spaced >= DESPACE_MIN_PAIRS:
        tight = len(_TIGHT_PAIR.findall(line))
        if spaced >= DESPACE_MIN_RATIO * (spaced + tight):
            return _SPACED_PAIR.sub("", line)
    return _CIRCLED_STRAY_SPACE.sub(r"\1", line)


def despace_cjk_text(text: str) -> str:
    """Apply `despace_cjk()` line by line (newlines preserved)."""
    return "\n".join(despace_cjk(ln) for ln in text.split("\n"))


# -----------------------------------------------------------------------------
# Promoting section headings (only lines listed in the TOC)
# -----------------------------------------------------------------------------
# PDF section headings like 「Ⅱ．曝露部位の洗浄法」「1．診察」 become plain lines in pdftotext.
# Without a heading the HTML page has no id, and the model cannot point to the section
# with `#heading` (2026-10-05 <redacted>).
#
# Deciding by "short line starting with roman/arabic numeral + ．" alone would also promote
# numbered list items (「２．注射薬採用の変更時は、」「２．発生日時：〔　年　月〕」). A wrong
# promotion is worse than a miss, so promote **only when a numbered entry in the document's
# own TOC matches a whole line located after the TOC**. Two TOC shapes are recognized:
#   - with leaders:  `Ⅱ．曝露部位の洗浄法 ....... 3` (most of the infection control manual)
#   - numbered lines right after a 「目次」 line (medical safety 「麻酔科 病棟・外来編」)
# Matching walks the TOC in order, one entry at a time (looking up to `_TOC_LOOKAHEAD` ahead).
# Even if the same 「1. 一般的対策」 appears twice under Ⅱ and Ⅲ, each matches once, in TOC order.
# Documents without a TOC are untouched (numbered lines stay plain).
_ROMAN = "ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩⅪⅫ"
_TOC_NUMBERED = re.compile(rf"^(?P<num>[{_ROMAN}]+|[0-9０-９]{{1,2}})[ \t　]*[．.][ \t　]*(?P<rest>\S.*)$")
# TOC line ending with leaders (… / ・・・ / ....) + page number. At least 3 leader characters
_TOC_LEADER = re.compile(r"^\s*(?P<t>\S.*?)\s*[.．…‥・･]{3,}\s*[0-9０-９]{1,4}\s*$")
_TOC_TRAILING_PAGE = re.compile(r"\s{2,}[0-9０-９]{1,4}$")
_TOC_HEAD = re.compile(r"^\s*[【\[]?\s*目\s*次\s*[】\]]?\s*$")
_PAGE_MARK = re.compile(r"^【p\.\d+】$")
_TOC_LOOKAHEAD = 6
HEADING_MAX_CHARS = 60  # longer lines are never headings (guards against a body sentence matching the TOC)


def _toc_key(s: str) -> str:
    """Match key: NFKC + whitespace removed + trailing 「：」 removed (「1．病院内における食中毒の原因：」)."""
    s = unicodedata.normalize("NFKC", s)
    return re.sub(r"\s+", "", s).rstrip(":")


def _toc_entries(lines: list[str]) -> tuple[list[tuple[str, str]], set[int]]:
    """Numbered TOC entries [(kind, key)] and the set of line indices read as TOC.

    kind is "roman" / "arabic". Unnumbered TOC entries (「曝露時の対応フローチャート」) are not
    used for matching. Entries wrapped over 2 lines (`Ⅵ．…場合の連` + `絡・報告 ..... 4`) join
    the leader-bearing 2nd line onto the 1st to form one entry.
    """
    entries: list[tuple[str, str]] = []
    toc_lines: set[int] = set()
    in_block = False
    prev_numbered: int | None = None  # index of the previous line if it was numbered (no leaders)

    def kind_of(t: str) -> str:
        nm = _TOC_NUMBERED.match(t)
        return "roman" if nm.group("num")[0] in _ROMAN else "arabic"

    for i, ln in enumerate(lines):
        s = ln.strip()
        if _TOC_HEAD.match(ln):
            in_block, prev_numbered = True, None
            toc_lines.add(i)
            continue
        m = _TOC_LEADER.match(ln)
        if m:
            t = m.group("t").strip()
            if _TOC_NUMBERED.match(t):
                entries.append((kind_of(t), _toc_key(t)))
            elif prev_numbered is not None:
                head = lines[prev_numbered].strip()
                if prev_numbered in toc_lines and entries and entries[-1][1] == _toc_key(head):
                    entries[-1] = (entries[-1][0], _toc_key(head + t))
                else:
                    entries.append((kind_of(head), _toc_key(head + t)))
                toc_lines.add(prev_numbered)
            toc_lines.add(i)
            prev_numbered = None
            continue
        if in_block:
            # The shape right after 「目次」 may have no leaders and only a page number at the right
            # edge (`Ⅰ．判定基準          3`). Drop only the number after 2+ spaces
            s = _TOC_TRAILING_PAGE.sub("", s)
        nm = _TOC_NUMBERED.match(s) if s and len(s) <= HEADING_MAX_CHARS else None
        if in_block:
            if not s:
                continue
            if nm:
                entries.append((kind_of(s), _toc_key(s)))
                toc_lines.add(i)
                prev_numbered = i
                continue
            in_block = False
        prev_numbered = i if nm else None
    return entries, toc_lines


_SKIP_LINE = re.compile(r"^\s*(?:[|#>!]|[-*+]\s|```|【p\.)")


def promote_toc_headings(body: str) -> tuple[str, list[str]]:
    """Turn section heading lines listed in the TOC into `##` / `###` headings. Returns (body, promoted headings).

    - Level: roman numeral = `##`, arabic = `###` (if the TOC has no roman numerals, arabic becomes `##`)
    - The heading text is the line with surrounding whitespace removed and inner runs of whitespace
      collapsed to one. An entry wrapped over 2 lines in the TOC is joined with the next body line
      when that makes it match the TOC
    - When the same heading would appear twice in a document, the 2nd and later get
      ` — <nearest preceding ## heading>` appended to avoid duplicates (better to read which section
      it is under than to let the HTML id escape with `-2`). Existing headings (from the overlay's
      VLM-reproduced pages) are not duplicated either
    - TOC lines themselves, lines before the first TOC, and table / list / quote / code / existing
      heading / 【p.N】 lines are excluded
    """
    lines = body.split("\n")
    entries, toc_lines = _toc_entries(lines)
    if not entries:
        return body, []
    first_toc = min(toc_lines)
    has_roman = any(k == "roman" for k, _ in entries)
    used = {
        re.sub(r"^#+\s*", "", ln).strip()
        for ln in lines
        if re.match(r"^#{1,6}\s", ln)
    }
    out: list[str] = []
    promoted: list[str] = []
    ptr = 0
    parent = ""
    in_fence = False
    skip_next = False
    for i, ln in enumerate(lines):
        if skip_next:
            skip_next = False
            continue
        if ln.lstrip().startswith("```"):
            in_fence = not in_fence
        s = ln.strip()
        if (
            i <= first_toc or i in toc_lines or in_fence or ptr >= len(entries)
            or _SKIP_LINE.match(ln) or not s or len(s) > HEADING_MAX_CHARS
            or not _TOC_NUMBERED.match(s)
        ):
            out.append(ln)
            continue
        key = _toc_key(s)
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        nxt_ok = bool(nxt) and not _SKIP_LINE.match(lines[i + 1]) and not _TOC_NUMBERED.match(nxt)
        hit, joined = None, False
        for j in range(ptr, min(ptr + _TOC_LOOKAHEAD, len(entries))):
            ek = entries[j][1]
            if ek == key:
                hit = j
                break
            if nxt_ok and ek != key and ek.startswith(key) and ek == key + _toc_key(nxt):
                hit, joined = j, True
                break
        if hit is None:
            out.append(ln)
            continue
        ptr = hit + 1
        kind = entries[hit][0]
        level = "##" if kind == "roman" or not has_roman else "###"
        text = re.sub(r"\s+", " ", s + nxt if joined else s)
        skip_next = joined
        if level == "##":
            parent = text
        if text in used and level == "###" and parent:
            text = f"{text} — {parent}"
        n = 2
        base = text
        while text in used:
            text = f"{base} ({n})"
            n += 1
        used.add(text)
        if out and out[-1].strip():
            out.append("")
        out.append(f"{level} {text}")
        promoted.append(f"{level} {text}")
        follow = i + (2 if joined else 1)
        if follow < len(lines) and lines[follow].strip():
            out.append("")
            # A line indented 4+ spaces right after a heading becomes indented code in markdown.
            # Before promotion it was a continuation line of the paragraph following the heading,
            # so strip only the leading whitespace and keep it a paragraph (later indented lines stay continuations)
            if re.match(r"^ {4,}\S", lines[follow]):
                lines[follow] = lines[follow].lstrip()
    return "\n".join(out), promoted


# -----------------------------------------------------------------------------
# Node names (path segments)
# -----------------------------------------------------------------------------
# File and directory names under data/doc become the node path as is, and the model writes
# the path as the URL `/ext/doc/<path>.md` (= the sandbox path) inside its citation `[…]`
# (2026-10-05 <redacted>; migrated from `/ext/cage/doc/<path>` on 2026-10-06).
# Japanese is fine as is, but whitespace and ASCII that breaks URLs/markdown stay out of names:
#   - whitespace (ASCII, full-width, runs) → `_`. Whitespace adjacent to an existing `_` or to
#     full-width punctuation (・、（）「」 etc.) is removed instead (never produce `_ _` or `）_頸部`)
#   - `[ ] # ? % & + " < > \ | ^ ` { }` → the same character in full-width
# The same rule is linted in cage/cage/build.py (`PATH_HOSTILE`); names fixed here are not
# warned about there (scripts/test_textfix.py cross-checks this).
PATH_HOSTILE_ASCII = '[]#?%&+"<>\\|^`{}'
_TO_FULLWIDTH = {ord(c): chr(ord(c) + 0xFEE0) for c in PATH_HOSTILE_ASCII}
_PUNCT_NEAR_SPACE = "_・、。，．：；！？（）「」『』【】［］〔〕〈〉《》"
_SPACE_AROUND_PUNCT = re.compile(rf"\s*([{re.escape(_PUNCT_NEAR_SPACE)}])\s*")


def safe_segment(name: str) -> str:
    """Make one node name (file stem / directory name) URL-safe."""
    s = name.translate(_TO_FULLWIDTH).strip()
    s = _SPACE_AROUND_PUNCT.sub(r"\1", s)
    s = re.sub(r"\s+", "_", s)
    return s


# -----------------------------------------------------------------------------
# Doc paths and figure/original URLs written in the body
# -----------------------------------------------------------------------------
# When the body (and front matter) points to another node, write the canonical form
# `{{DOC_BASE}}/<category>/…/<name>.md` (directories: `{{DOC_BASE}}/<category>/…/`); figures and
# originals are `{{DOC_STATIC_BASE}}/<relative path>`. Each placeholder has exactly one value, and
# the cage build (FIXED_PLACEHOLDERS in cage/cage/build.py) substitutes `/ext/doc` `/ext/doc-static`.
# Never write the old prefix-less form `/<category>/…` (the build does not rewrite by guessing;
# old forms that resolve to a node show up in the manifest's noncanonical_links).
DOC_BASE = "{{DOC_BASE}}"
DOC_STATIC_BASE = "{{DOC_STATIC_BASE}}"


def doc_ref(path: str, *, is_dir: bool) -> str:
    """Doc path (`/<category>/…`; `.md` or trailing `/` optional) → canonical form written in the body.

    Leaves are `{{DOC_BASE}}/…/<name>.md`, directories `{{DOC_BASE}}/…/`. The caller passes the node
    kind it knows (a node it wrote itself, or a real file in the existing tree).
    """
    p = "/" + path.strip("/")
    if p.endswith(".md"):
        p = p[: -len(".md")]
    if p == "/":
        return DOC_BASE + "/"
    return DOC_BASE + p + ("/" if is_dir else ".md")


def static_ref(rel: str) -> str:
    """Relative path of a delivered figure/original (under `data/static`) → URL written in the body."""
    return DOC_STATIC_BASE + "/" + rel.lstrip("/")


# -----------------------------------------------------------------------------
# Delivered file names (data/static)
# -----------------------------------------------------------------------------
# The model also copies delivered-file URLs from the body as whole `[…](…)`. Long romanized
# original names (`mitourokukanzyazizentourokumoushikomisyo.pdf`) get copied with 1-2 characters
# dropped and 404 (2026-10-05; the label copies correctly, only the URL breaks). Delivered names
# are short ASCII: **a short key derived from a stable attribute of the original**, not its literal string.
STATIC_NAME = re.compile(r"[A-Za-z0-9._-]+")


def static_key(text: str, n: int = 4) -> str:
    """String → n-digit hex key (part of a delivered file name). Pure and deterministic.

    sha1 is taken after NFKC + whitespace normalization, so full/half-width and spacing
    variations do not change it. Collisions within the same parent are detected by the caller
    (growing n would rename every existing file, so on collision stop and let a human decide).
    """
    norm = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).strip()
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:n]
