#!/usr/bin/env python3
"""Manifests → doc tree. The default is the guidelines published by the Japanese Society of Neurology (`診療ガイドライン/神経疾患/`).

**Corpus-independent**. Corpus-specific values (manifests / PDFs / output root / section index text) are
confined to `Corpus`, registered in `CORPORA` and switched with `use_corpus()`. Another corpus only needs
one thin wrapper (e.g. `scripts/build_cancer_registry.py`). `<corpus>` below is `jns_guidelines` by default.

Input:
  - `scripts/<corpus>/<id>.yaml` … 1 document = 1 manifest (tracked in git, written by a human)
  - `cache/<corpus>/pdf/<file>`  … source PDFs (for JNS, scripts/fetch_jns_guidelines.py provides them)
  - `data/ingest/<corpus>/overlays/<id>/<slug>-pNNN.md` … if a markdown reproduction of the page by a VLM
    exists it is used instead of pdftotext (optional)
  - `data/ingest/<corpus>/figs.json` … captions / pages of cropped figures (optional).
    `![図: …]` is embedded at the end of the page body (cropping is done by scripts/crop_jns_figs.py)

Output:
  - `<out_root>/<id>/…`                      … nodes (idempotent; <id>/ is rebuilt every time)
  - `<out_root>/_index.md`                   … section _index (the contents list is generated from the manifests)
  - `data/static/<corpus>/docs/<slug>`  … served source PDFs (linked from the end of each node; orphans are removed)
  - `data/ingest/<corpus>/vlm_candidates.json` … work list for the later step (VLM transcription)

Relation to precedents (docs/cage/13 medical safety / docs/cage/15 infection control):
  - Those built the tree from a table-of-contents HTML, but here **the source has no machine-readable TOC**
    (JNS is just 190 PDFs laid out flat), so a human (subagent) writes the tree into the manifest.
    The build only reads the manifest and makes no decisions
  - Page splitting, 【p.N】 markers, overlay substitution and source links with `{{DOC_STATIC_BASE}}` follow the same style
  - The body follows the original text. No line joining, no rephrasing (only headers / footers and setext accident lines are dropped)

The manifest format is in `docs/cage/22_jns_guidelines.md`. Two things **not yet documented** there:

`pdfs[].slug` and `pdfs[].url` (2026-09-11):
  **The served name (`slug`) and the provenance (`url`) are different things**. At least one is required.

  - `url` only … the basename is the served name (as before)
  - `slug` only … that is the served name. The node's `source` falls back to the manifest-level `source`
  - both … **`slug` is the served name**, `source` is `url`. They need not agree — for sources whose
    society site uses a 32-digit hash as basename (dermatology), a readable served name is given separately

  The served name appears in `data/static/<corpus>/docs/<slug>` and in the source link at the end of the body;
  the overlay filename (`<slug stem>-pNNN.md`) and `slug` in figs.json are aligned to it as well.
  It must be an ASCII `.pdf` name (`[A-Za-z0-9._-]+`) and **unique within the corpus** (static is a flat
  `docs/<slug>` with no id directory in between; main validates across manifests).

`normalize_radicals:` — collapse radical / vertical-text glyphs (2026-09-11):
  `normalize_radicals: true` at manifest level (default false). Some sources mix Kangxi radical glyphs
  (`⾏` U+2F8F / `⽤` U+2F64 / `⽣` U+2F63) or vertical-text symbols (`︓` U+FE13) into the body, and
  quoting the `read_doc` output as-is yields 「⽣存状況」 (`grep_doc` applies NFKC, so search itself works).
  When true, **only characters in U+2E80–U+2EF3 / U+2F00–U+2FD5 / U+FE10–U+FE19** are folded to NFKC one
  by one (full-width alphanumerics, ligatures and circled numbers are untouched).
  It is applied **right after pdftotext extraction** (before clean / strip / strip_edge / from・until) and
  **right after loading an overlay**. Hence the `strip` / `strip_edge` regexes and `patches.find` are
  written in the **normalised glyphs** (`:` rather than `︓`; accepting both in a character class keeps
  them working when the flag is toggled).

`substitutions:` — fix character mix-ups in the text layer (2026-09-12):
  The embedded font's ToUnicode may be broken so that **the same error is scattered over every page**
  (in the dermatology 『掌蹠角化症診療の手引き』, 「腱」 comes out as `伳` U+4F73). At manifest level:

      substitutions:
        - pdf: 掌蹠角化症診療の手引き.pdf   # optional. Omitted = applies to every PDF of the manifest
          find: '伳'                       # literal (not a regex)
          replace: '腱'
          note: 'ToUnicode 誤り。紙面は腱'   # optional

  - Applied **right after pdftotext extraction**, **after** `normalize_radicals` and before clean / strip.
    Hence `find` is written in the normalised glyphs
  - **Not applied to overlays** (an overlay is the canon written by a human / VLM looking at the page)
  - Any count is accepted (0 hits passes with **only a warning** — so a fix in a revised edition is noticed).
    The summary line shows `subs=<applied count>`
  - Difference from `patches:`: a patch fixes "one spot on one page exactly once" and errors unless the
    match count is 1. substitutions fix "the same error scattered over every page" regardless of count.
    `patches.find` is written in the text **after** substitutions are applied

Normalising spaced-out lines (2026-09-12):
  For lines the source typeset with justification, pdftotext returns 「小 脳 失 調 症」 split character by character.
  The walker's search does not collapse whitespace, so `grep_doc` finds 0 hits although the text is there.
  At the end of `clean_page()`, `textfix.despace_cjk` is applied line by line to close them (no manifest
  setting; always on). Only lines with 3 or more 「CJK␣CJK」 that also make up 40% or more of all adjacent
  CJK pairs are affected; spaces next to digits / Latin letters and column alignment (2+ spaces) are structurally untouched.
  **Not applied to overlays** (text reproduced from the page by a human; form titles are typeset that way on the page).
  It is applied after strip / strip_edge, so `from` / `until` / `patches.find` match the text
  **after** closing. The rationale for the thresholds is in docs/cage/22_jns_guidelines.md.

`body[].figs: false` — attach figures of a shared page to one side only (2026-10-06):
  Figures in figs.json are attached to (slug, page), so the figures of a page shared by 2 nodes via
  `allow_overlap` are embedded in **both nodes**. If a figure belongs to one CQ, write `figs: false` on the
  other node's segment so no figures are embedded there (default true). It is per segment, so first cut the
  shared page out into a one-page segment and then set it. Case: figure 4 (CQ1) on p.45 of chapter 6 of the
  dermatology 『褥瘡』 also appeared on the CQ2 side.

`patches:` — apply revision-history diffs (2026-09-11):
  Reflect a "revision history" the publisher issued after the source was published. Optional, at manifest level:

      patches:
        - pdf: 2016manual.pdf   # a file declared in pdfs:
          page: 63              # physical page
          find: '…'             # literal string occurring exactly once in the final body of that page
          replace: ''           # replacement ('' = delete)
          note: '修正履歴 2026-07-27 (印字 p.57) の【削除】を反映'   # optional

  - Applied to **the final text of that page** (after clean; the overlay text if the page has an overlay),
    before figures `![図: …]` are attached
  - `find` is a **literal** match (neither regex nor NFKC-normalised). The source may use Kangxi radical
    glyphs (`⾒` U+2F52) etc., so paste the pdftotext output as-is
  - 0 or 2+ matches is an **error** both in `--check` and in the build (never silently unapplied — so that
    someone who later writes an overlay and restores a line that should have been deleted is detected)
  - If `note` is given, one quote line `> [修正履歴反映] <note>` is inserted at the replacement position
    (even with `replace: ''`, so the reader can tell a line of the source was removed here).
    The quote line always starts at a line start, and a blank line is added only when the following line
    would be pulled into the quote (markdown lazy continuation)
  - Several patches on the same page are applied in manifest order (the next one sees the previous result)
  - `pdf` not in pdfs: / `page` outside the PDF / the page not in any node — all are errors

usage:
    uv run python scripts/build_jns_guidelines.py            # build all manifests
    uv run python scripts/build_jns_guidelines.py --check    # validate only (generate nothing)
    uv run python scripts/build_jns_guidelines.py --only 頭痛2021
    uv run python scripts/build_jns_guidelines.py --manifests /tmp/.../groupA
    uv run python scripts/build_cancer_registry.py --check    # another corpus (thin wrapper)
  (run from inside cage/. Needs pdftotext / pdfinfo (poppler-utils))
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import yaml

sys.path.insert(0, str(Path(__file__).parent))
# Repairs of pdftotext-derived text show up in the same form across corpora, so they are consolidated
# in `textfix.py` (2026-09-12). Do not write same-named implementations back here — fix textfix and its tests instead.
from textfix import despace_cjk, doc_ref, fix_radicals, static_ref, strip_invisible  # noqa: E402

# Closed schema the section (the corpus entry node) imposes on the <id> directly below. Each manifest's
# `<id>/_index.md` carries all 4 items (written by build_one)
SECTION_FIELDS = ["guideline: str", "publisher: str", "source?: str", "retrieved_at?: date"]

JNS_RETRIEVED_AT = "2026-09-05"
CANCER_RETRIEVED_AT = "2026-09-11"
JDA_RETRIEVED_AT = "2026-09-07"

JNS_SECTION_BODY = """\
日本神経学会が「刊行物・ガイドライン」ページで公開している診療ガイドライン・診断基準・
診療の手引き類 ({retrieved} 取得)。1 ディレクトリ = 1 ガイドラインで、CQ を持つものは
**1 ノード = 1 CQ** (推奨 + 解説 + 文献まで自己完結)、CQ でない章は 1 ノード = 1 節。

- 各ノードの `pages` は原本 PDF の**物理ページ**範囲 (目次の印字ページではない)。
  本文中の【p.N】が各ページの先頭。`source` はそのノードの原本 PDF の URL
- 本文は PDF テキスト層の抽出 (原文準拠。要約・言い換え・行の結合はしていない)。
  表・アルゴリズム図はレイアウトが崩れていることがある
- `> (このページはテキスト層がほぼ無く…)` が入っているページは図・表が主体で、
  本文からは中身が読めない。**原本 PDF を見るか、その旨を添えて答える**
- 各ノード末尾の「原本 PDF」は該当ページに `#page=N` で飛ぶ原本へのリンク
- **収録は現行版のみ**。同じ束にある旧版は落としてある (どれを落としたかは各
  ガイドラインの `_index.md` 末尾「収録にあたっての判断」)。追補版は親ガイドラインの
  子セクションとして収録している

## 収録

{toc}

## ライセンス

学会サイトで公開されている PDF を取り込んだもの。**転載・再配布は日本神経学会および
各発行社 (医学書院・南江堂 等) の許諾対象**であり、本データはローカル技術検証用途に限る。
外部公開・再配布はしない。本番運用前に発行元への照会が必要。
"""

CANCER_SECTION_BODY = """\
がん登録 (院内がん登録・全国がん登録) の登録実務で使う標準登録様式と届出マニュアル
({retrieved} 取得)。1 ディレクトリ = 1 資料。収録しているのは次の 3 種:

- 国立がん研究センター がん登録センター「がん診療連携拠点病院等 院内がん登録
  標準登録様式 2016 年版」(2026-04-01 更新版に、修正履歴 2026-07-27 を反映したもの)
- 同「院内がん登録 2006 年度版修正版 解釈本」— **2015 年 12 月以前に診断された症例**に
  適用される旧版。2016 年版と併存するので、症例の診断日で使い分ける
- 厚生労働省・国立がん研究センター「全国がん登録 届出マニュアル 2026」

- 各ノードの `pages` は原本 PDF の**物理ページ**範囲 (印字ページではない)。
  本文中の【p.N】が各ページの先頭
- 本文は PDF テキスト層の抽出 (原文準拠。要約・言い換え・行の結合はしていない)。
  表・フロー図はレイアウトが崩れていることがある
- `> (このページはテキスト層がほぼ無く…)` が入っているページは図・表が主体で、
  本文からは中身が読めない。**原本 PDF を見るか、その旨を添えて答える**
- `> [修正履歴反映] …` の引用行は、原本の発行後に発行元が出した修正履歴を反映した箇所
- 各ノード末尾の「原本 PDF」は該当ページに `#page=N` で飛ぶ原本へのリンク

## 収録

{toc}

## ライセンス

国立がん研究センター・厚生労働省が公開している PDF を取り込んだもの。本データは
ローカル技術検証用途に限り、外部公開・再配布はしない。本番運用前に発行元への照会が必要。
"""


JDA_SECTION_BODY = """\
日本皮膚科学会が「ガイドライン・指針」のページに掲載している診療ガイドライン・
診療の手引き・使用ガイダンス類 ({retrieved} 取得)。1 ディレクトリ = 1 資料で、CQ を持つ
ものは **1 ノード = 1 CQ** (推奨 + 解説 + 文献まで自己完結)、CQ でない章は 1 ノード = 1 節。

- 各ノードの `pages` は原本 PDF の**物理ページ**範囲 (目次の印字ページではない)。
  本文中の【p.N】が各ページの先頭。`source` はそのノードの原本 PDF の URL
- 本文は PDF テキスト層の抽出 (原文準拠。要約・言い換え・行の結合はしていない)。
  表・アルゴリズム図はレイアウトが崩れていることがある
- `> (このページはテキスト層がほぼ無く…)` が入っているページは図・表が主体で、
  本文からは中身が読めない。**原本 PDF を見るか、その旨を添えて答える**
- 各ノード末尾の「原本 PDF」は該当ページに `#page=N` で飛ぶ原本へのリンク
- **収録は現行版のみ**。同じ束にある旧版は落としてある (どれを落としたかは各
  資料の `_index.md` 末尾「収録にあたっての判断」)。補遺版・追記は親資料の
  子セクションとして収録している

このセクション特有の形:

- **皮膚悪性腫瘍**のガイドライン 2025 年版 (メラノーマ・有棘細胞癌・基底細胞癌・
  メルケル細胞癌・乳房外パジェット病・皮膚血管肉腫・皮膚リンパ腫) は、本体のほかに
  **CQ ごとの「推奨決定資料」**(システマティックレビューの資料。定性的・定量的
  システマティックレビュー、エビデンス総体、推奨決定の経過) が別 PDF で付く。
  推奨と解説は本体、その根拠の詳細は推奨決定資料にある — **推奨文の出どころを
  問われたら推奨決定資料まで見る**
- **創傷・褥瘡・熱傷ガイドライン (2023)** は 6 分冊 (創傷一般 / 褥瘡 / 糖尿病性皮膚潰瘍・
  壊疽 / 膠原病・血管炎にともなう皮膚潰瘍 / 下腿潰瘍・下肢静脈瘤 / 熱傷) で、分冊ごとに
  CQ 番号が振り直される。**分冊を指定せずに「CQ3」と言わない**
- **蕁麻疹診療ガイドライン 2026 (第 4 版)** は本体 + 別表 3 本 + 簡略版という構成
- ガイドラインのほか、**薬剤の使用ガイダンス・安全使用マニュアル・適正使用の手引き・
  診療の手引き**も学会の掲載物として同じセクションに入っている (生物学的製剤・JAK 阻害薬・
  ヒドロキシクロロキン 等)。投与前スクリーニングや副作用対応を引くのはこちら
- 他学会との**合同ガイドライン** (血管腫・脈管奇形、乾癬性関節炎 等) も、日本皮膚科学会が
  掲載しているものはここに入れている

## 収録

{toc}

## ライセンス

学会サイトで公開されている PDF を取り込んだもの。**転載・再配布は日本皮膚科学会および
各発行社の許諾対象**であり、本データはローカル技術検証用途に限る。外部公開・再配布は
しない。本番運用前に発行元への照会が必要。他学会との合同ガイドラインは各学会の
公表条件に従う。
"""


@dataclass
class Corpus:
    """1 corpus = 1 section. Corpus-specific values shared by build / profile / crop.

    Locations are derived from `name` (`scripts/<name>/` `cache/<name>/`
    `data/ingest/<name>/` `data/static/<name>/`). The only per-corpus differences are
    **the output root and the section index text**.
    """

    name: str
    out_root: Path  # output root in the doc tree (a section under a category, or a category directly under root)
    section_title: str
    section_body: str  # body template with {retrieved} / {toc} filled in
    retrieved_at: str  # default when the manifest does not give retrieved_at
    # Front matter of the section _index.md (between title and fields). Write **only what satisfies
    # the closed schema imposed by the parent**. For a category directly under root, the root `_index.yaml`
    # has no fields = children may only have title, so this is empty (docs/cage/01_brief.md)
    section_meta: list[str] = field(default_factory=list)

    @property
    def manifest_dir(self) -> Path:
        return Path("scripts") / self.name

    @property
    def pdf_dir(self) -> Path:
        return Path("cache") / self.name / "pdf"

    @property
    def pages_dir(self) -> Path:
        """Page images made by profile (shown to the VLM)."""
        return Path("cache") / self.name / "pages"

    @property
    def figs_out(self) -> Path:
        """Raw output of the VLM subagent (input to crop)."""
        return Path("cache") / self.name / "figs_out"

    @property
    def ingest(self) -> Path:
        return Path("data/ingest") / self.name

    @property
    def overlays(self) -> Path:
        return self.ingest / "overlays"

    @property
    def vlm_json(self) -> Path:
        return self.ingest / "vlm_candidates.json"

    @property
    def figs_json(self) -> Path:
        return self.ingest / "figs.json"

    @property
    def profile_json(self) -> Path:
        return self.ingest / "page_profile.json"

    @property
    def gaps_json(self) -> Path:
        return self.ingest / "page_gaps.json"

    @property
    def batch_dir(self) -> Path:
        return self.ingest / "vlm_batches"

    @property
    def static_docs(self) -> Path:
        return Path("data/static") / self.name / "docs"

    @property
    def static_figs(self) -> Path:
        return Path("data/static") / self.name / "figs"

    @property
    def static_url(self) -> str:
        """URL prefix embedded in the body (`{{DOC_STATIC_BASE}}/<corpus>`, followed by `/docs/...` `/figs/...`)."""
        return static_ref(self.name)


CORPORA: dict[str, Corpus] = {
    "jns_guidelines": Corpus(
        name="jns_guidelines",
        out_root=Path("data/doc/診療ガイドライン/神経疾患"),
        section_title="神経疾患",
        section_body=JNS_SECTION_BODY,
        retrieved_at=JNS_RETRIEVED_AT,
        # The parent (`診療ガイドライン/_index.md`) requires guideline/publisher/source/retrieved_at
        section_meta=[
            "guideline: '日本神経学会 掲載 診療ガイドライン・診断基準・診療の手引き'",
            "publisher: 日本神経学会",
            "source: https://www.neurology-jp.org/guidelinem/index.html",
            f"retrieved_at: '{JNS_RETRIEVED_AT}'",
        ],
    ),
    "cancer_registry": Corpus(
        name="cancer_registry",
        out_root=Path("data/doc/がん登録"),
        section_title="がん登録",
        section_body=CANCER_SECTION_BODY,
        retrieved_at=CANCER_RETRIEVED_AT,
        # Category directly under root. The root `_index.yaml` has no fields = title only
        section_meta=[],
    ),
    "jda_guidelines": Corpus(
        name="jda_guidelines",
        out_root=Path("data/doc/診療ガイドライン/皮膚疾患"),
        section_title="皮膚疾患",
        section_body=JDA_SECTION_BODY,
        retrieved_at=JDA_RETRIEVED_AT,
        # Like JNS, a section under the parent (`診療ガイドライン/_index.md`) = the same 4 items
        section_meta=[
            "guideline: '日本皮膚科学会 掲載 診療ガイドライン・診療の手引き・使用ガイダンス'",
            "publisher: 日本皮膚科学会",
            "source: https://www.dermatol.or.jp/modules/guideline/index.php?content_id=1",
            f"retrieved_at: '{JDA_RETRIEVED_AT}'",
        ],
    ),
}

# Current corpus. Default is JNS (so the existing CLI keeps working as-is).
CORPUS = CORPORA["jns_guidelines"]


def corpus() -> Corpus:
    """Current corpus. **Other scripts call this** (importing the constant would miss the switch)."""
    return CORPUS


def use_corpus(name: str) -> Corpus:
    """Switch the corpus (called from a thin wrapper / `--corpus`)."""
    global CORPUS
    if name not in CORPORA:
        sys.exit(f"unknown corpus: {name} (available: {sorted(CORPORA)})")
    CORPUS = CORPORA[name]
    _page_cache.clear()  # the PDF cache is keyed by filename only
    _count_cache.clear()
    return CORPUS
# Every level under <id> has these 2 items (chapter _index.md and leaves alike).
# `?` = provenance not shown in /doc/list (docs/cage/01_brief.md)
NODE_FIELDS = ["source?: str", "pages?: str"]

# Characters not allowed in a path segment. `/ \ :` break paths; whitespace and parentheses break URLs and link syntax.
# `# % & + ^ `` ` also break URLs (2026-10-05; includes the same set as the lint `PATH_HOSTILE` in cage/cage/build.py).
# (title is free — write searchable words as-is)
BAD_SEGMENT = re.compile(r'[/\\:*?"<>|()（）\[\]{}#%&+^`\s]')

# Removal of invisible characters (zero-width, C0 controls) is `textfix.strip_invisible`. The JDA journal
# typesetting puts BEL (U+0007) into wrapped headings, which would appear in the body if not dropped
# (`CQ7 <U+200C><BEL> 菌状息肉症への…`). U+000C (form feed) passes through **after** pages_of has split
# pages on `\f`, so it may be dropped there.
_RULE_LINE = re.compile(r"^[ \t]*[-=_]{3,}[ \t]*$")  # setext accident (docs/cage/07_markdown_setext_trap.md)
# A line-initial `#` in the text layer is not a heading (table column headers in the source such as 「# 検索式 文献数」).
# Left as-is it turns into an ATX heading, and in the HTML version, which uses heading text as id, headings
# within a node collide (the search-formula table of a dermatology CQ appears 3 times in one node). Escape to `\#` to keep the text
_ATX_LIKE = re.compile(r"^([ \t]{0,3})(#{1,6})(?=[ \t]|$)")

# pdftotext modes. default = no flags (single-column text comes out cleanly),
# raw = content stream order (rescue for PDFs where the first line of a paragraph lands at the page end),
# layout = keep the visual columns (for checking 2-column layouts and tables)
MODES = {"default": [], "raw": ["-raw"], "layout": ["-layout"]}

# A page with fewer characters than this after strip is treated as "almost no text layer" (figure / table page)
THIN_PAGE_CHARS = 40
# Default for strip_edge (how many non-blank lines at the page head / tail it applies to)
EDGE_LINES_DEFAULT = 3


# -----------------------------------------------------------------------------
# manifest
# -----------------------------------------------------------------------------
@dataclass
class Seg:
    """One body segment = a contiguous page range of one PDF (inclusive) + optional in-page split."""

    pdf: str
    start: int
    end: int
    frm: re.Pattern | None = None  # first page: take from the first matching line
    until: re.Pattern | None = None  # last page: cut just before the matching line
    mode: str = ""  # "" = the default mode of that PDF
    allow_overlap: bool = False  # may share pages with other nodes (intentional overlap)
    figs: bool = True  # false = do not embed figs.json figures on this segment's pages (shared-page figures on one side only)


@dataclass
class Clean:
    """Cleaning settings for one PDF (merge of manifest level + pdfs[])."""

    strip: list[re.Pattern] = field(default_factory=list)  # fullmatch on every line
    edge: list[re.Pattern] = field(default_factory=list)  # fullmatch only on the head / tail non-blank lines
    edge_lines: int = EDGE_LINES_DEFAULT


@dataclass
class Sub:
    """One literal substitution fixing a character mix-up in the text layer (manifest-level `substitutions:`)."""

    find: str
    replace: str
    pdf: str = ""  # "" = every PDF of the manifest
    note: str = ""


@dataclass
class Patch:
    """One revision-history diff (manifest-level `patches:`). Details in the module docstring."""

    pdf: str
    page: int
    find: str  # literal string occurring **exactly once** in the final text of that page
    replace: str = ""
    note: str = ""


@dataclass
class Node:
    path: str
    title: str
    body: list[Seg] = field(default_factory=list)
    body_text: str = ""
    children: list["Node"] = field(default_factory=list)


@dataclass
class Manifest:
    id: str
    title: str
    guideline: str
    publisher: str
    source: str
    retrieved_at: str
    description: str
    normalize_radicals: bool  # fold radical / vertical-text glyphs to NFKC (default false)
    pdfs: dict[str, dict]  # file -> {url, slug, label, mode, clean}
    nodes: list[Node]
    vlm: list[dict]
    subs: list[Sub]
    patches: list[Patch]
    notes: str
    text_caveat: str
    src: Path


def _pages(v, where: str) -> tuple[int, int]:
    """Accepts `pages` as 5 / [5] / [5, 9] (inclusive)."""
    if isinstance(v, int):
        return v, v
    if isinstance(v, list) and len(v) == 1 and isinstance(v[0], int):
        return v[0], v[0]
    if isinstance(v, list) and len(v) == 2 and all(isinstance(x, int) for x in v):
        if v[0] > v[1]:
            raise ValueError(f"{where}: pages start > end: {v}")
        return v[0], v[1]
    raise ValueError(f"{where}: pages must be an int, [start] or [start, end]: {v!r}")


def _regex(v, where: str) -> re.Pattern:
    try:
        return re.compile(str(v))
    except re.error as e:
        raise ValueError(f"{where}: broken regular expression: {v!r} — {e}") from None


def _patterns(v, where: str) -> list[re.Pattern]:
    return [_regex(s, f"{where}[{i}]") for i, s in enumerate(v or [])]


def _strip_edge(raw, where: str, fallback_lines: int) -> tuple[list[re.Pattern], int]:
    """`strip_edge: {lines: N, patterns: [...]}`. Uses fallback when lines is omitted."""
    if raw is None:
        return [], fallback_lines
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: strip_edge must be a {{lines, patterns}} mapping")
    for k in raw:
        if k not in {"lines", "patterns"}:
            raise ValueError(f"{where}: unknown key in strip_edge {k!r}")
    lines = raw.get("lines", fallback_lines)
    if not isinstance(lines, int) or lines < 1:
        raise ValueError(f"{where}: strip_edge.lines must be an integer >= 1: {lines!r}")
    return _patterns(raw.get("patterns"), f"{where}.strip_edge.patterns"), lines


def _mode(v, where: str) -> str:
    m = str(v).strip()
    if m not in MODES:
        raise ValueError(f"{where}: mode must be one of {sorted(MODES)}: {v!r}")
    return m


_NODE_KEYS = {"path", "title", "body", "body_text", "children"}
_SEG_KEYS = {"pdf", "pages", "from", "until", "mode", "allow_overlap", "figs"}


def _node(raw: dict, where: str, pdf_names: set[str]) -> Node:
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: node is not a mapping: {raw!r}")
    for k in raw:
        if k not in _NODE_KEYS:
            raise ValueError(f"{where}: unknown key {k!r} (allowed: {sorted(_NODE_KEYS)})")
    path = str(raw.get("path") or "").strip()
    title = str(raw.get("title") or "").strip()
    if not path or not title:
        raise ValueError(f"{where}: path and title are required")
    if BAD_SEGMENT.search(path):
        raise ValueError(f"{where}: path contains a disallowed character: {path!r}")
    if path.startswith(".") or path in {"_index", "_index.md", "_index.yaml"}:
        raise ValueError(f"{where}: path is a reserved name / hidden file: {path!r}")

    body = []
    for i, b in enumerate(raw.get("body") or []):
        w = f"{where}/{path}.body[{i}]"
        if not isinstance(b, dict) or "pdf" not in b or "pages" not in b:
            raise ValueError(f"{w}: {{pdf, pages}} required: {b!r}")
        for k in b:
            if k not in _SEG_KEYS:
                raise ValueError(f"{w}: unknown key {k!r} (allowed: {sorted(_SEG_KEYS)})")
        pdf = unicodedata.normalize("NFC", str(b["pdf"]).strip())
        if pdf not in pdf_names:
            raise ValueError(f"{w}: PDF not declared in pdfs:: {pdf}")
        s, e = _pages(b["pages"], w)
        body.append(
            Seg(
                pdf,
                s,
                e,
                frm=_regex(b["from"], f"{w}.from") if b.get("from") else None,
                until=_regex(b["until"], f"{w}.until") if b.get("until") else None,
                mode=_mode(b["mode"], w) if b.get("mode") else "",
                allow_overlap=bool(b.get("allow_overlap")),
                figs=bool(b.get("figs", True)),
            )
        )

    body_text = str(raw.get("body_text") or "").strip()
    kids = [_node(c, f"{where}/{path}", pdf_names) for c in (raw.get("children") or [])]
    seen: set[str] = set()
    for k in kids:
        if k.path in seen:
            raise ValueError(f"{where}/{path}: duplicate child path: {k.path}")
        seen.add(k.path)
    if not body and not body_text and not kids:
        raise ValueError(f"{where}/{path}: no body, body_text or children (empty node)")
    return Node(path, title, body, body_text, kids)


_MANIFEST_KEYS = {
    "id", "title", "guideline", "publisher", "source", "retrieved_at",
    "description", "strip", "strip_edge", "text_caveat", "pdfs", "nodes",
    "vlm_candidates", "substitutions", "patches", "notes", "normalize_radicals",
}
_PDF_KEYS = {"file", "url", "slug", "label", "mode", "strip", "strip_edge"}
_PATCH_KEYS = {"pdf", "page", "find", "replace", "note"}
_SUB_KEYS = {"pdf", "find", "replace", "note"}


def load_manifest(p: Path) -> Manifest:
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{p}: top level of the YAML is not a mapping")
    for k in raw:
        if k not in _MANIFEST_KEYS:
            raise ValueError(f"{p}: unknown key {k!r} (allowed: {sorted(_MANIFEST_KEYS)})")
    for k in ("id", "title", "guideline", "publisher", "source", "description", "pdfs", "nodes"):
        if not raw.get(k):
            raise ValueError(f"{p}: {k} is missing")
    mid = unicodedata.normalize("NFC", str(raw["id"]).strip())
    if mid != unicodedata.normalize("NFC", p.stem):
        raise ValueError(f"{p}: id ({mid}) does not match the filename ({p.stem})")
    if BAD_SEGMENT.search(mid):
        raise ValueError(f"{p}: id contains a disallowed character: {mid!r}")

    nr = raw.get("normalize_radicals", False)
    if not isinstance(nr, bool):
        raise ValueError(f"{p}: normalize_radicals must be true / false: {nr!r}")

    g_strip = _patterns(raw.get("strip"), f"{p.stem}.strip")
    g_edge, g_edge_lines = _strip_edge(raw.get("strip_edge"), p.stem, EDGE_LINES_DEFAULT)

    pdfs: dict[str, dict] = {}
    for i, e in enumerate(raw["pdfs"]):
        w = f"{p.stem}.pdfs[{i}]"
        if not isinstance(e, dict) or "file" not in e or not (e.get("url") or e.get("slug")):
            raise ValueError(
                f"{w}: {{file, url}} required (a PDF without url needs slug: for the served name): {e!r}"
            )
        for k in e:
            if k not in _PDF_KEYS:
                raise ValueError(f"{w}: unknown key {k!r} (allowed: {sorted(_PDF_KEYS)})")
        f = unicodedata.normalize("NFC", str(e["file"]).strip())
        if f in pdfs:
            raise ValueError(f"{p}: the same file appears twice in pdfs: {f}")
        # **The served name (slug) and the provenance (url) are different things**. If `slug:` is given it is the
        # served name and `url:` only remains as the node's `source` (some society sites use a 32-digit hash as
        # basename, unusable as a served name). Without `slug:` the url basename is used; without `url:`
        # `source` falls back to the manifest-level source (write_node)
        url = str(e.get("url") or "").strip()
        slug = str(e.get("slug") or "").strip()
        from_slug = bool(slug)
        if not slug and url:
            slug = Path(urlparse(url).path).name
        if not slug.lower().endswith(".pdf") or not re.fullmatch(r"[A-Za-z0-9._-]+", slug):
            raise ValueError(
                f"{w}: cannot derive an ASCII PDF name: "
                + (f"slug={slug!r}" if from_slug else f"url={url}")
            )
        p_edge, p_edge_lines = _strip_edge(e.get("strip_edge"), w, g_edge_lines)
        pdfs[f] = {
            "url": url,
            "slug": slug,
            "label": str(e.get("label") or Path(f).stem),
            "mode": _mode(e["mode"], w) if e.get("mode") else "default",
            # manifest level and pdfs[] are merged (per-PDF additions, not overrides)
            "clean": Clean(
                strip=g_strip + _patterns(e.get("strip"), f"{w}.strip"),
                edge=g_edge + p_edge,
                edge_lines=p_edge_lines,
            ),
        }
    slugs = [v["slug"] for v in pdfs.values()]
    if len(set(slugs)) != len(slugs):
        raise ValueError(f"{p}: URL basenames collide (served names would not be unique): {slugs}")

    subs: list[Sub] = []
    for i, e in enumerate(raw.get("substitutions") or []):
        w = f"{p.stem}.substitutions[{i}]"
        if not isinstance(e, dict):
            raise ValueError(f"{w}: substitution is not a mapping: {e!r}")
        for k in e:
            if k not in _SUB_KEYS:
                raise ValueError(f"{w}: unknown key {k!r} (allowed: {sorted(_SUB_KEYS)})")
        if not e.get("find"):
            raise ValueError(f"{w}: find is missing")
        sf = unicodedata.normalize("NFC", str(e["pdf"]).strip()) if e.get("pdf") else ""
        if sf and sf not in pdfs:
            raise ValueError(f"{w}: PDF not declared in pdfs:: {sf}")
        subs.append(
            Sub(
                find=str(e["find"]),
                replace=str(e.get("replace") or ""),
                pdf=sf,
                note=str(e.get("note") or "").strip(),
            )
        )

    patches: list[Patch] = []
    for i, e in enumerate(raw.get("patches") or []):
        w = f"{p.stem}.patches[{i}]"
        if not isinstance(e, dict):
            raise ValueError(f"{w}: patch is not a mapping: {e!r}")
        for k in e:
            if k not in _PATCH_KEYS:
                raise ValueError(f"{w}: unknown key {k!r} (allowed: {sorted(_PATCH_KEYS)})")
        for k in ("pdf", "page", "find"):
            if e.get(k) in (None, ""):
                raise ValueError(f"{w}: {k} is missing")
        pf = unicodedata.normalize("NFC", str(e["pdf"]).strip())
        if pf not in pdfs:
            raise ValueError(f"{w}: PDF not declared in pdfs:: {pf}")
        if not isinstance(e["page"], int) or e["page"] < 1:
            raise ValueError(f"{w}: page must be an integer >= 1 (physical page): {e['page']!r}")
        # find / replace are **literal**. Neither NFKC nor regex is applied (see docstring)
        patches.append(
            Patch(
                pdf=pf,
                page=e["page"],
                find=str(e["find"]),
                replace=str(e.get("replace") or ""),
                note=str(e.get("note") or "").strip(),
            )
        )

    nodes = [_node(n, p.stem, set(pdfs)) for n in raw["nodes"]]
    seen: set[str] = set()
    for n in nodes:
        if n.path in seen:
            raise ValueError(f"{p}: duplicate top-level path: {n.path}")
        seen.add(n.path)

    return Manifest(
        id=mid,
        title=str(raw["title"]).strip(),
        guideline=str(raw["guideline"]).strip(),
        publisher=str(raw["publisher"]).strip(),
        source=str(raw["source"]).strip(),
        retrieved_at=str(raw.get("retrieved_at") or CORPUS.retrieved_at).strip(),
        description=str(raw["description"]).rstrip() + "\n",
        normalize_radicals=nr,
        pdfs=pdfs,
        nodes=nodes,
        vlm=list(raw.get("vlm_candidates") or []),
        subs=subs,
        patches=patches,
        notes=str(raw.get("notes") or "").strip(),
        text_caveat=str(raw.get("text_caveat") or "").strip(),
        src=p,
    )


def walk(nodes: list[Node]):
    for n in nodes:
        yield n
        yield from walk(n.children)


# -----------------------------------------------------------------------------
# PDF
# -----------------------------------------------------------------------------
_page_cache: dict[tuple[str, str], list[str]] = {}
_count_cache: dict[str, int] = {}


def pdf_path(file: str) -> Path:
    return CORPUS.pdf_dir / file


def n_pages(file: str) -> int:
    if file not in _count_cache:
        out = subprocess.run(
            ["pdfinfo", str(pdf_path(file))], capture_output=True, text=True
        ).stdout
        m = re.search(r"^Pages:\s+(\d+)", out, re.M)
        _count_cache[file] = int(m.group(1)) if m else 0
    return _count_cache[file]


def pages_of(file: str, mode: str = "default") -> list[str]:
    """Extract the whole PDF in one go and split on `\\f` (two orders of magnitude faster than page by page).

    Returns a list with one dummy element prepended so it can be indexed 1-based.
    Extracted once per mode and cached.
    """
    key = (file, mode)
    if key not in _page_cache:
        text = subprocess.run(
            ["pdftotext", *MODES[mode], str(pdf_path(file)), "-"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        parts = text.split("\f")
        if parts and not parts[-1].strip():
            parts.pop()  # after the trailing \f
        if len(parts) != n_pages(file):
            # If the \f split and pdfinfo disagree, every later page is off by one (a silent mix-up)
            print(
                f"  ! {file} ({mode}): pdftotext page count {len(parts)} differs from pdfinfo "
                f"{n_pages(file)}",
                file=sys.stderr,
            )
        _page_cache[key] = [""] + parts
    return _page_cache[key]


def page_text(seg: Seg, m: Manifest, page: int) -> str:
    """Extract one page with the segment's mode and cleaning settings (before in-page split)."""
    info = m.pdfs[seg.pdf]
    pages = pages_of(seg.pdf, seg.mode or info["mode"])
    text = pages[page] if page < len(pages) else ""
    # Glyph normalisation goes **before clean** (strip / strip_edge / from・until are written against the
    # normalised glyphs. The pages_of cache is manifest-independent, so apply it here)
    if m.normalize_radicals:
        text = fix_radicals(text)
    # Character mix-up fixes also go before clean (find is written in the glyphs after normalize_radicals)
    if m.subs:
        text, _ = apply_subs(m, seg.pdf, text)
    return clean_page(text, info["clean"])


def clean_page(raw: str, cfg: Clean) -> str:
    """Drop headers / footers (strip / strip_edge) and setext accident lines, leaving only the body.

    **No line joining or rephrasing** (faithful to the original). What is dropped:
      - lines that fullmatch `strip` (all lines)
      - lines that fullmatch `strip_edge.patterns` — but **only the first / last `edge_lines`
        non-blank lines of the page**. Applying the page number `\\d{1,3}` to every line would
        also catch numeric table cells (ALSFRS-R raw scores, MGFA scores, search hit counts)
      - lines consisting only of `---` `===` `___` (they turn the previous line into a heading; docs/cage/07)
    plus escaping a line-initial `#` (would become an ATX heading) to `\\#`,
    plus trailing whitespace, invisible characters (zero-width and C0 controls), compression of 3+ blank lines,
    and closing **justification-spaced characters** (`textfix.despace_cjk`). Nothing else.

    Despacing is applied **after** `strip` / `strip_edge` (the manifest regexes are written against the
    text exactly as pdftotext emits it). `from` / `until` and `find` of `patches:` match the text after
    despacing — if a manifest relies on a spaced-out line as a marker, `--check` fails with "0 matches",
    so it is noticed.
    **Not applied to overlays** (page reproductions written by humans, with no spaced-out characters).
    """
    kept: list[str] = []
    for ln in raw.split("\n"):
        ln = strip_invisible(ln).rstrip()
        if _RULE_LINE.match(ln):
            continue
        s = ln.strip()
        if s and any(p.fullmatch(s) for p in cfg.strip):
            continue
        kept.append(ln)

    if cfg.edge:
        # Count the edges on the lines **after strip**. Page numbers are often printed next to the
        # running head (chapter / book title); counted on raw lines they get pushed out of the window
        # by the running head (GBS: the tail is in the order [..., '159', 'ギラン・バレー症候群', '第 9 章'])
        nonblank = [i for i, ln in enumerate(kept) if ln.strip()]
        edge = set(nonblank[: cfg.edge_lines]) | set(nonblank[-cfg.edge_lines :])
        kept = [
            ln
            for i, ln in enumerate(kept)
            if not (i in edge and ln.strip() and any(p.fullmatch(ln.strip()) for p in cfg.edge))
        ]
    return re.sub(
        r"\n{3,}", "\n\n", "\n".join(_ATX_LIKE.sub(r"\1\\\2", despace_cjk(ln)) for ln in kept)
    ).strip("\n")


# Collapsing Kangxi radicals, CJK radical supplements and vertical-text symbols is `textfix.fix_radicals`.
# Here it is called only for manifests with `normalize_radicals: true` (see docstring)


def apply_subs(m: Manifest, pdf: str, text: str) -> tuple[str, list[int]]:
    """Apply `substitutions:` in manifest order. (text, hit count per substitution).

    Any count is accepted (0 passes). `--check` turns "never matched" into a warning.
    """
    hits = []
    for sb in m.subs:
        if sb.pdf and sb.pdf != pdf:
            hits.append(0)
            continue
        c = text.count(sb.find)
        if c:
            text = text.replace(sb.find, sb.replace)
        hits.append(c)
    return text, hits


def is_thin(text: str) -> bool:
    """Is the page thin after clean (= mostly figures / tables, content unreadable from the text layer)?

    The build inserts a note to that effect in the body, and `scripts/profile_jns_pages.py` uses this as
    the primary source of VLM candidates. **Shared so the test is not written in 2 places**.
    """
    return len(re.sub(r"\s", "", text)) < THIN_PAGE_CHARS


def splice(text: str, pt: Patch) -> str:
    """Replace `find` (exactly one spot) with `replace`, and insert one quote line if `note` is given.

    The quote line always starts at a line start. A blank line is added only when the following line
    would be pulled into the quote by markdown lazy continuation (a body line right after `> …` continues the quote).
    """
    i = text.index(pt.find)
    head, tail = text[:i], text[i + len(pt.find):]
    ins = pt.replace
    if pt.note:
        if ins and not ins.endswith("\n"):
            ins += "\n"
        ins += f"> [修正履歴反映] {pt.note}"
        if head and not head.endswith("\n"):
            ins = "\n" + ins
        if tail.strip() and not tail.startswith("\n\n"):
            ins += "\n" if tail.startswith("\n") else "\n\n"
    return head + ins + tail


def apply_patches(m: Manifest, pdf: str, page: int, text: str, where: str) -> tuple[str, int]:
    """Apply the patches for that page in manifest order. (text, applied count).

    ValueError unless the match count is 1. Leaving it silently unapplied would hide that someone who
    later wrote an overlay restored a line that should have been deleted.
    """
    k = 0
    for pt in m.patches:
        if pt.pdf != pdf or pt.page != page:
            continue
        hits = text.count(pt.find)
        if hits != 1:
            raise ValueError(
                f"patches: find matched {hits} times (must be exactly once) — "
                f"{pdf} p.{page} ({where}): {pt.find!r}"
            )
        text = splice(text, pt)
        k += 1
    return text, k


def slice_page(text: str, pat: re.Pattern, *, tail: bool) -> tuple[str, bool]:
    """In-page split by `from` / `until`. Returns (cut text, whether it matched).

    tail=False (`from`): from the first matching line to the end.
    tail=True  (`until`): from the start to **just before** the matching line.
    """
    lines = text.split("\n")
    for i, ln in enumerate(lines):
        if pat.search(ln):
            cut = lines[:i] if tail else lines[i:]
            return "\n".join(cut).strip("\n"), True
    return text, False


# -----------------------------------------------------------------------------
# Validation
# -----------------------------------------------------------------------------
def check(m: Manifest) -> tuple[list[str], list[str], int, int]:
    """(errors, warnings, patches applied, substitution hits). No build if there are errors."""
    errs: list[str] = []
    warns: list[str] = []

    for f in m.pdfs:
        if not pdf_path(f).exists():
            errs.append(f"PDF not in cache: {f} (fetch with scripts/fetch_jns_guidelines.py)")

    used: dict[str, set[int]] = {f: set() for f in m.pdfs}
    dup: dict[str, set[int]] = {f: set() for f in m.pdfs}
    for n in walk(m.nodes):
        for seg in n.body:
            if not pdf_path(seg.pdf).exists():
                continue
            total = n_pages(seg.pdf)
            if seg.start < 1 or seg.end > total:
                errs.append(
                    f"{n.path}: page range {seg.start}-{seg.end} of {seg.pdf} is "
                    f"outside the {total} pages"
                )
                continue
            for p in range(seg.start, seg.end + 1):
                if p in used[seg.pdf] and not seg.allow_overlap:
                    dup[seg.pdf].add(p)
                used[seg.pdf].add(p)
            # For from / until, "believed to cut but not cutting" is the most dangerous case, so
            # actually extract and verify the match (this is the real point of --check)
            if seg.frm:
                _, hit = slice_page(page_text(seg, m, seg.start), seg.frm, tail=False)
                if not hit:
                    errs.append(
                        f"{n.path}: from does not match ({seg.pdf} p.{seg.start}): "
                        f"{seg.frm.pattern!r}"
                    )
            if seg.until:
                _, hit = slice_page(page_text(seg, m, seg.end), seg.until, tail=True)
                if not hit:
                    errs.append(
                        f"{n.path}: until does not match ({seg.pdf} p.{seg.end}): "
                        f"{seg.until.pattern!r}"
                    )
    if not list(walk(m.nodes)):
        errs.append("nodes is empty")

    for f, ps in dup.items():
        if ps:
            warns.append(
                f"{f}: pages included in several nodes {_ranges(sorted(ps))} "
                "(if intentional, allow_overlap: true)"
            )

    for f in m.pdfs:
        if not pdf_path(f).exists():
            continue
        missing = sorted(set(range(1, n_pages(f) + 1)) - used[f])
        if not missing:
            continue
        if m.notes:
            warns.append(f"{f}: pages in no node {_ranges(missing)} (passed as covered by notes)")
        else:
            errs.append(
                f"{f}: pages in no node {_ranges(missing)} — "
                "include them or state the reason for dropping them in notes"
            )

    for i, v in enumerate(m.vlm):
        w = f"vlm_candidates[{i}]"
        if not isinstance(v, dict) or "pdf" not in v or "pages" not in v:
            errs.append(f"{w}: {{pdf, pages, why}} required")
            continue
        f = unicodedata.normalize("NFC", str(v["pdf"]).strip())
        if f not in m.pdfs:
            errs.append(f"{w}: PDF not in pdfs: {f}")
            continue
        try:
            s, e = _pages(v["pages"], w)
        except ValueError as ex:
            errs.append(str(ex))
            continue
        if pdf_path(f).exists() and (s < 1 or e > n_pages(f)):
            errs.append(f"{w}: {s}-{e} outside {f} ({n_pages(f)} pages)")

    # substitutions: count the hits on the included pages (0 is a warning; a sign the revision fixed it)
    n_subs = 0
    if m.subs:
        per = [0] * len(m.subs)
        seen: set[tuple[str, int]] = set()
        for node in walk(m.nodes):
            for seg in node.body:
                if not pdf_path(seg.pdf).exists() or seg.end > n_pages(seg.pdf):
                    continue
                info = m.pdfs[seg.pdf]
                pages = pages_of(seg.pdf, seg.mode or info["mode"])
                for pg in range(seg.start, seg.end + 1):
                    if (seg.pdf, pg) in seen:
                        continue
                    seen.add((seg.pdf, pg))
                    raw_text = pages[pg] if pg < len(pages) else ""
                    if m.normalize_radicals:
                        raw_text = fix_radicals(raw_text)
                    _, hits = apply_subs(m, seg.pdf, raw_text)
                    per = [a + b for a, b in zip(per, hits)]
        n_subs = sum(per)
        for i, c in enumerate(per):
            if c == 0:
                sb = m.subs[i]
                warns.append(
                    f"substitutions[{i}]: {sb.find!r} → {sb.replace!r} never matches on the "
                    "included pages (the source was fixed / wrong character in find)"
                )

    # patches: verify the declarations and "exactly once in the final text" by **actually applying them**
    # (same path as the build; on pages with an overlay the overlay text is the target)
    n_patch = 0
    if m.patches:
        overlays = load_overlays(m.id, m.normalize_radicals)
        occ: dict[tuple[str, int], list[tuple[str, Seg]]] = {}
        for n in walk(m.nodes):
            for seg in n.body:
                for p in range(seg.start, seg.end + 1):
                    occ.setdefault((seg.pdf, p), []).append((n.path, seg))
        pages: list[tuple[str, int]] = []
        for i, pt in enumerate(m.patches):
            w = f"patches[{i}]"
            if not pdf_path(pt.pdf).exists():
                continue
            if pt.page > n_pages(pt.pdf):
                errs.append(f"{w}: p.{pt.page} outside {pt.pdf} ({n_pages(pt.pdf)} pages)")
            elif (pt.pdf, pt.page) not in occ:
                errs.append(
                    f"{w}: {pt.pdf} p.{pt.page} is in no node (does not appear in the body)"
                )
            elif (pt.pdf, pt.page) not in pages:
                pages.append((pt.pdf, pt.page))
        for f, p in pages:
            for path, seg in occ[(f, p)]:
                text, _ = final_page_text(m, seg, p, overlays)
                try:
                    _, k = apply_patches(m, f, p, text, path)
                    n_patch += k
                except ValueError as ex:
                    errs.append(str(ex))
    return errs, warns, n_patch, n_subs


def _ranges(pages: list[int]) -> str:
    """[1,2,3,7] -> '1-3, 7' (for reporting missing pages)."""
    spans: list[list[int]] = []
    for p in pages:
        if spans and p == spans[-1][1] + 1:
            spans[-1][1] = p
        else:
            spans.append([p, p])
    return ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in spans)


# -----------------------------------------------------------------------------
# Output
# -----------------------------------------------------------------------------
def yaml_str(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def quote_block(s: str) -> str:
    return "\n".join(f"> {ln}" if ln.strip() else ">" for ln in s.split("\n"))


def pages_field(m: Manifest, segs: list[Seg]) -> str:
    """`pages` in the front matter. '3-5, 9' for one PDF, 'slug:3-5, slug2:1-2' for several."""
    if not segs:
        return ""
    multi = len({s.pdf for s in segs}) > 1
    out = []
    for s in segs:
        r = f"{s.start}-{s.end}" if s.end > s.start else str(s.start)
        out.append(f"{Path(m.pdfs[s.pdf]['slug']).stem}:{r}" if multi else r)
    return ", ".join(out)


def span_of(n: Node) -> list[Seg]:
    """The node's own body; if none, gather the bodies below it (to synthesise a chapter's pages/source)."""
    if n.body:
        return n.body
    out: list[Seg] = []
    for c in n.children:
        out.extend(span_of(c))
    if not out:
        return []
    # Fold contiguous segments of the same PDF into one (so a chapter's pages does not list one range per CQ)
    merged = [Seg(out[0].pdf, out[0].start, out[0].end)]
    for s in out[1:]:
        last = merged[-1]
        if s.pdf == last.pdf and s.start <= last.end + 1:
            last.end = max(last.end, s.end)
        else:
            merged.append(Seg(s.pdf, s.start, s.end))
    return merged


def load_overlays(mid: str, normalize: bool = False) -> dict[tuple[str, int], str]:
    """overlays/<id>/<slug stem>-pNNN.md → markdown that replaces that page.

    If `normalize` (= the manifest's `normalize_radicals`) is true, glyphs are normalised
    **right after loading** (the VLM may have copied the source glyphs as-is).
    """
    out: dict[tuple[str, int], str] = {}
    d = CORPUS.overlays / mid
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.md")):
        m = re.fullmatch(r"(?P<slug>.+)-p(?P<page>\d+)", f.stem)
        if not m:
            print(f"  ! cannot parse overlay name: {f}", file=sys.stderr)
            continue
        text = strip_invisible(f.read_text(encoding="utf-8").strip("\n"))
        out[(m.group("slug"), int(m.group("page")))] = fix_radicals(text) if normalize else text
    return out


def load_figs(mid: str) -> dict[tuple[str, int], list[dict]]:
    """figs.json → figure list per (slug stem, page). Empty if absent (figures are an optional artefact).

    Shape: `[{"id": ..., "slug": "als_2023", "page": 55,
           "figs": [{"caption": ..., "bbox_pct": [x0,y0,x1,y1] | "full": true, "rotate": 0}]}]`
    Cropping is done by `scripts/crop_jns_figs.py` (keep the served-name rule aligned with it).
    """
    if not CORPUS.figs_json.exists():
        return {}
    out: dict[tuple[str, int], list[dict]] = {}
    for e in json.loads(CORPUS.figs_json.read_text(encoding="utf-8")):
        if unicodedata.normalize("NFC", str(e["id"])) != unicodedata.normalize("NFC", mid):
            continue
        out[(Path(str(e["slug"])).stem, int(e["page"]))] = e["figs"]
    return out


THIN_NOTE = "> (このページはテキスト層がほぼ無く、図・表として原本 PDF を参照してください)"


def final_page_text(
    m: Manifest, seg: Seg, page: int, overlays: dict[tuple[str, int], str]
) -> tuple[str, str]:
    """Return the final text of one page (before patches, before figures) and its origin.

    Origin: `overlay` = replaced by an overlay / `thin` = almost no text layer, note added /
    `""` = pdftotext text as-is. **Both build and --check go through here**.

    The in-page split (`from` / `until`) **also applies to pages taken from an overlay** (2026-09-11).
    Otherwise, as soon as an overlay lands on a page split across 2 nodes, the whole page appears in
    both nodes. If it does not match on the overlay side, **warn and take the whole page** —
    the overlay author may have changed the heading wording, so do not drop silently
    (`--check` still verifies from/until against the raw pdftotext as before).
    """
    slug_stem = Path(m.pdfs[seg.pdf]["slug"]).stem
    key = (slug_stem, page)
    if key in overlays:
        text = overlays[key]
        for pat, tail in ((seg.frm, False), (seg.until, True)):
            if not pat or page != (seg.end if tail else seg.start):
                continue
            text, hit = slice_page(text, pat, tail=tail)
            if not hit:
                print(
                    f"  ! {m.id}: {'until' if tail else 'from'} does not match "
                    f"in the overlay ({slug_stem} p.{page}): {pat.pattern!r} "
                    "— taking the whole page",
                    file=sys.stderr,
                )
        return text, "overlay"
    text = page_text(seg, m, page)
    # The "thin text layer" test is done on the **whole page** (so a fragment that is short only
    # because of the in-page split is not mistaken for a figure page)
    thin = is_thin(text)
    # The in-page split applies to the cleaned text (= cut what appears in the output)
    if seg.frm and page == seg.start:
        text, _ = slice_page(text, seg.frm, tail=False)
    if seg.until and page == seg.end:
        text, _ = slice_page(text, seg.until, tail=True)
    if thin:
        text = (text + "\n\n" if text else "") + THIN_NOTE
        return text, "thin"
    return text, ""


def render(
    m: Manifest,
    n: Node,
    overlays: dict[tuple[str, int], str],
    figs: dict[tuple[str, int], list[dict]],
    st: dict,
) -> tuple[str, str]:
    """Return (body, source PDF links). The body is the pages of each segment joined with 【p.N】 separators.

    The links are returned separately so that chapter nodes can insert `## 収録` (list of children)
    and still **always put the links last** (for leaves they are simply concatenated).
    """
    blocks: list[str] = []
    if m.text_caveat:
        blocks.append(quote_block(m.text_caveat))
    if n.body_text:
        blocks.append(n.body_text)

    for seg in n.body:
        slug_stem = Path(m.pdfs[seg.pdf]["slug"]).stem
        for p in range(seg.start, seg.end + 1):
            key = (slug_stem, p)
            text, kind = final_page_text(m, seg, p, overlays)
            if kind == "overlay":
                st["overlay_used"] += 1
            elif kind == "thin":
                st["thin"] += 1
            # Revision history is applied **to the final text**, before figures (same path as --check)
            text, n_pat = apply_patches(m, seg.pdf, p, text, n.path)
            st["patches"] += n_pat
            block = [f"【p.{p}】"]
            if text:
                block.append(text)
            # Figures go after the body (read the text, then look at the figures). Names follow the same rule as crop_jns_figs.py
            # Segments with `figs: false` get no figures (so figures of a page shared by 2 nodes via allow_overlap
            # appear only in the node they belong to). The number k follows the in-page order in figs.json
            for k, fig in enumerate(figs.get(key, []) if seg.figs else [], 1):
                block.append(
                    f"![図: {fig['caption']}]"
                    f"({CORPUS.static_url}/figs/"
                    f"{slug_stem}-p{p:03d}-f{k}.jpg)"
                )
                st["figs"] += 1
            blocks.append("\n\n".join(block))
            st["pages"] += 1

    # The served URL is only the ASCII slug (`docs/<slug>`). It used to be `docs/<id>/<slug>`, which put
    # 100+ character percent-encoded Japanese <id>s like `%E7%A5%9E…` into the body, and they got
    # garbled when the model copied them (2026-10-05). The slug is unique within the corpus (main validates)
    tail = []
    for seg in n.body:
        info = m.pdfs[seg.pdf]
        rng = f"p.{seg.start}–{seg.end}" if seg.end > seg.start else f"p.{seg.start}"
        tail.append(
            f"原本 PDF: [{info['label']} {rng}]"
            f"({CORPUS.static_url}/docs/{info['slug']}"
            f"#page={seg.start})"
        )
    return "\n\n".join(blocks), "\n\n".join(tail)


def write_node(m: Manifest, n: Node, parent: Path, overlays, figs, st: dict) -> None:
    """Write the node directly under parent. `<path>/_index.md` if it has children, else `<path>.md`."""
    segs = span_of(n)
    # A PDF without url (a source whose official URL cannot be determined) falls back to the manifest-level source
    src = (m.pdfs[segs[0].pdf]["url"] or m.source) if segs else m.source
    fm = [
        "---",
        f"title: {yaml_str(n.title)}",
        f"source: {src}",
        f"pages: {yaml_str(pages_field(m, segs))}",
    ]
    body, link = render(m, n, overlays, figs, st)
    st["nodes"] += 1

    if not n.children:
        st["leaves"] += 1
        fm.append("---")
        text = "\n".join(fm) + "\n\n" + "\n\n".join(x for x in (body, link) if x) + "\n"
        (parent / f"{n.path}.md").write_text(unicodedata.normalize("NFC", text), encoding="utf-8")
        return

    d = parent / n.path
    d.mkdir(parents=True, exist_ok=True)
    fm += ["fields:"] + [f"  {f}" for f in NODE_FIELDS] + ["---"]
    toc = "## 収録\n\n" + "\n".join(f"- `{c.path}` — {c.title}" for c in n.children)
    text = "\n".join(fm) + "\n\n" + "\n\n".join(x for x in (body, toc, link) if x) + "\n"
    (d / "_index.md").write_text(unicodedata.normalize("NFC", text), encoding="utf-8")
    for c in n.children:
        write_node(m, c, d, overlays, figs, st)


def build_one(m: Manifest, warns: int) -> dict:
    out = CORPUS.out_root / m.id
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    # Served PDFs are `docs/<slug>` (no id directory in between). Remove the old layout `docs/<id>/`
    # if it remains. Other ids and orphan slugs removed from manifests are cleaned up by main
    static = CORPUS.static_docs
    if (static / m.id).is_dir():
        shutil.rmtree(static / m.id)
    static.mkdir(parents=True, exist_ok=True)

    overlays = load_overlays(m.id, m.normalize_radicals)
    figs = load_figs(m.id)
    st = {
        "id": m.id, "nodes": 0, "leaves": 0, "pages": 0, "pdfs": len(m.pdfs),
        "overlay_used": 0, "overlay_want": len(overlays), "thin": 0, "figs": 0, "patches": 0,
        "fig_want": sum(len(v) for v in figs.values()), "warns": warns,
    }

    fm = [
        "---",
        f"title: {yaml_str(m.title)}",
        f"guideline: {yaml_str(m.guideline)}",
        f"publisher: {yaml_str(m.publisher)}",
        f"source: {m.source}",
        f"retrieved_at: {yaml_str(m.retrieved_at)}",
        "fields:",
        *[f"  {f}" for f in NODE_FIELDS],
        "---",
    ]
    toc = "\n".join(f"- `{n.path}` — {n.title}" for n in m.nodes)
    head = quote_block(m.text_caveat) + "\n\n" if m.text_caveat else ""
    body = head + m.description.rstrip() + "\n\n## 収録\n\n" + toc + "\n"
    if m.notes:
        body += "\n## 収録にあたっての判断\n\n" + m.notes + "\n"
    (out / "_index.md").write_text(
        unicodedata.normalize("NFC", "\n".join(fm) + "\n\n" + body), encoding="utf-8"
    )

    for n in m.nodes:
        write_node(m, n, out, overlays, figs, st)

    for f, info in m.pdfs.items():
        shutil.copyfile(pdf_path(f), static / info["slug"])

    if st["figs"] != st["fig_want"]:
        print(
            f"  ! {m.id}: {st['fig_want'] - st['figs']} figures could not be embedded "
            "(check that the slug/page in figs.json is within the manifest's node ranges)",
            file=sys.stderr,
        )
    if st["overlay_used"] != st["overlay_want"]:
        print(
            f"  ! {m.id}: {st['overlay_want'] - st['overlay_used']} overlays could not be applied "
            "(check that the slug/page in the filename is within the manifest's node ranges)",
            file=sys.stderr,
        )
    return st


def prune_static_docs(ms: list[Manifest]) -> int:
    """Make static docs/ contain exactly the slugs of all manifests (remove orphans).

    If a revision renames a slug, drops a PDF or removes an id and the old file remains, the old link
    "still opens", so the breakage goes unnoticed and the file rides the data bundle into the air-gapped site.
    All manifests are read even with `--only`, so other ids' files are not removed.
    """
    want = {info["slug"] for m in ms for info in m.pdfs.values()}
    d = CORPUS.static_docs
    if not d.is_dir():
        return 0
    n = 0
    for p in sorted(d.iterdir()):
        if p.is_dir():  # old layout docs/<id>/
            shutil.rmtree(p)
            n += 1
        elif p.name not in want:
            p.unlink()
            n += 1
    if n:
        print(f"  - static docs: removed {n} orphans ({d})", file=sys.stderr)
    return n


def write_vlm_json(ms: list[Manifest]) -> int:
    """Work list for the later step (VLM transcription). Pages are an expanded int list."""
    out: dict[str, list[dict]] = {}
    n = 0
    for m in sorted(ms, key=lambda x: x.id):
        rows = []
        for v in m.vlm:
            f = unicodedata.normalize("NFC", str(v["pdf"]).strip())
            s, e = _pages(v["pages"], "vlm")
            rows.append(
                {
                    "pdf": f,
                    "slug": m.pdfs[f]["slug"],
                    "pages": list(range(s, e + 1)),
                    "why": str(v.get("why") or ""),
                }
            )
            n += e - s + 1
        if rows:
            out[m.id] = rows
    CORPUS.vlm_json.parent.mkdir(parents=True, exist_ok=True)
    CORPUS.vlm_json.write_text(
        json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return n


def write_section_index(ms: list[Manifest]) -> None:
    """`_index.md` of the section (the corpus entry). The contents list is generated from the manifests.

    The front matter is **only what satisfies the closed schema imposed by the parent** (`section_meta`) +
    the `fields:` imposed on the children. Writing guideline etc. on a category directly under root makes
    the walker fail to start, since the root `_index.yaml` has no fields = only title is allowed (docs/cage/01_brief.md).
    """
    c = CORPUS
    c.out_root.mkdir(parents=True, exist_ok=True)
    toc = "\n".join(
        f"- `{m.id}` — {m.guideline} ({m.publisher})" for m in sorted(ms, key=lambda x: x.id)
    ) or "(収録待ち — manifest 未投入)"
    fm = [
        "---",
        f"title: {c.section_title}",
        *c.section_meta,
        "fields:",
        *[f"  {f}" for f in SECTION_FIELDS],
        "---",
    ]
    (c.out_root / "_index.md").write_text(
        unicodedata.normalize(
            "NFC",
            "\n".join(fm) + "\n\n" + c.section_body.format(retrieved=c.retrieved_at, toc=toc),
        ),
        encoding="utf-8",
    )


def print_table(rows: list[dict]) -> None:
    """Breakdown per id (a markdown table that can be pasted as-is into the "収録範囲" section of docs/cage/22)."""
    print()
    print("| id | nodes | leaves | pages | pdfs | overlays | thin | warn |")
    print("|---|--:|--:|--:|--:|--:|--:|--:|")
    for r in sorted(rows, key=lambda x: x["id"]):
        print(
            f"| `{r['id']}` | {r['nodes']} | {r['leaves']} | {r['pages']} | {r['pdfs']} | "
            f"{r['overlay_used']}/{r['overlay_want']} | {r['thin']} | {r['warns']} |"
        )
    tot = {k: sum(r[k] for r in rows) for k in
           ("nodes", "leaves", "pages", "pdfs", "overlay_used", "overlay_want", "thin", "warns")}
    print(
        f"| **total {len(rows)}** | {tot['nodes']} | {tot['leaves']} | {tot['pages']} | "
        f"{tot['pdfs']} | {tot['overlay_used']}/{tot['overlay_want']} | {tot['thin']} | "
        f"{tot['warns']} |"
    )
    print()


def main(doc: str = __doc__) -> None:
    ap = argparse.ArgumentParser(
        description=doc, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--manifests", type=Path, default=None,
        help="manifest directory (default: the corpus's scripts/<name>/)",
    )
    ap.add_argument("--only", metavar="ID", action="append", help="build only this id (repeatable)")
    ap.add_argument("--skip", metavar="ID", action="append", help="build all but this id (repeatable)")
    ap.add_argument("--check", action="store_true", help="validate only (generate nothing)")
    args = ap.parse_args()

    manifests_dir = args.manifests or CORPUS.manifest_dir
    if not manifests_dir.is_dir():
        sys.exit(f"manifest directory not found: {manifests_dir}")
    files = sorted(manifests_dir.glob("*.yaml"))
    if not files:
        # Allow the stage where only the section `_index.md` is set up first (manifests are being
        # written by subagents in parallel). Without it the walker cannot validate children against the parent fields
        print(
            f"  ! no manifest at all: {manifests_dir}/*.yaml "
            f"(writing only {CORPUS.section_title}/_index.md)",
            file=sys.stderr,
        )
        write_section_index([])
        print("ids=0 nodes=0 pages=0 pdfs=0 overlays=0/0")
        return

    manifests: list[Manifest] = []
    n_err = 0
    for p in files:
        try:
            manifests.append(load_manifest(p))
        except (ValueError, yaml.YAMLError) as e:
            print(f"  ! {p.name}: {e}", file=sys.stderr)
            n_err += 1
    if n_err:
        sys.exit(f"{n_err} manifests are broken")

    ids = {m.id for m in manifests}
    # Served names (slugs) are placed flat directly under static docs/, so they must be unique across the corpus
    owner: dict[str, str] = {}
    for m in manifests:
        for info in m.pdfs.values():
            if info["slug"] in owner:
                n_err += 1
                print(
                    f"  x slug {info['slug']} is duplicated in {owner[info['slug']]} and {m.id} "
                    "(served names are unique within the corpus; give one of them a slug:)",
                    file=sys.stderr,
                )
            owner[info["slug"]] = m.id
    if n_err:
        sys.exit(f"{n_err} duplicate slugs")
    targets = manifests
    if args.only:
        bad = set(args.only) - ids
        if bad:
            sys.exit(f"--only: no such id {sorted(bad)} (available ids: {sorted(ids)})")
        targets = [m for m in manifests if m.id in set(args.only)]
    if args.skip:
        bad = set(args.skip) - ids
        if bad:
            sys.exit(f"--skip: no such id {sorted(bad)}")
        targets = [m for m in targets if m.id not in set(args.skip)]
    if not targets:
        sys.exit("nothing to build")

    warns_by_id: dict[str, int] = {}
    patches_by_id: dict[str, int] = {}
    subs_by_id: dict[str, int] = {}
    for m in targets:
        errs, warns, n_patch, n_subs = check(m)
        for w in warns:
            print(f"  ! {m.id}: {w}", file=sys.stderr)
        warns_by_id[m.id] = len(warns)
        patches_by_id[m.id] = n_patch
        subs_by_id[m.id] = n_subs
        for e in errs:
            print(f"  x {m.id}: {e}", file=sys.stderr)
        n_err += len(errs)
    if n_err:
        sys.exit(f"{n_err} validation errors — not generating")
    if args.check:
        rows = [
            {
                "id": m.id,
                "nodes": sum(1 for _ in walk(m.nodes)),
                "leaves": sum(1 for n in walk(m.nodes) if not n.children),
                "pages": sum(s.end - s.start + 1 for n in walk(m.nodes) for s in n.body),
                "pdfs": len(m.pdfs),
                "overlay_used": 0,
                "overlay_want": len(load_overlays(m.id, m.normalize_radicals)),
                "thin": 0,
                "figs": 0,
                "patches": patches_by_id[m.id],
                "warns": warns_by_id[m.id],
            }
            for m in targets
        ]
        print_table(rows)
        print(
            f"check ok: ids={len(targets)} nodes={sum(r['nodes'] for r in rows)} "
            f"pages={sum(r['pages'] for r in rows)} pdfs={sum(r['pdfs'] for r in rows)} "
            f"patches={sum(r['patches'] for r in rows)} "
            f"subs={sum(subs_by_id[m.id] for m in targets)} "
            f"warnings={sum(r['warns'] for r in rows)}"
        )
        return

    if CORPUS.out_root.is_dir():
        for d in sorted(CORPUS.out_root.iterdir()):
            if d.is_dir() and d.name not in ids:
                print(
                    f"  ! a directory not in any manifest remains (left untouched): {d}",
                    file=sys.stderr,
                )

    rows = [build_one(m, warns_by_id[m.id]) for m in targets]
    prune_static_docs(manifests)
    write_section_index(manifests)
    n_vlm = write_vlm_json(manifests)

    print_table(rows)
    print(
        f"ids={len(rows)} nodes={sum(r['nodes'] for r in rows)} "
        f"pages={sum(r['pages'] for r in rows)} pdfs={sum(r['pdfs'] for r in rows)} "
        f"overlays={sum(r['overlay_used'] for r in rows)}/"
        f"{sum(r['overlay_want'] for r in rows)} "
        f"thin_pages={sum(r['thin'] for r in rows)} figs={sum(r['figs'] for r in rows)} "
        f"patches={sum(r['patches'] for r in rows)} "
        f"subs={sum(subs_by_id[m.id] for m in targets)} "
        f"vlm_pages={n_vlm}"
    )


if __name__ == "__main__":
    main()
