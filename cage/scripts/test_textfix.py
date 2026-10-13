"""Unit tests for textfix.py (repair of pdftotext-derived text).

Examples are taken from real data (medical safety `02_咬合系歯科.md:230` / `03_麻酔器の始業点検.md`,
infection control `2-1 標準予防策`, 疑義解釈 `K773-4`). Moving a threshold shows up as failures here.

usage: uv run python -m pytest scripts/test_textfix.py   (run from cage/)
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import textfix  # noqa: E402
from textfix import (  # noqa: E402
    despace_cjk,
    despace_cjk_text,
    fix_radicals,
    fix_symbol_bullets,
    promote_toc_headings,
    STATIC_NAME,
    safe_segment,
    static_key,
    strip_invisible,
)


# ---------------------------------------------------------------- strip_invisible


def test_strips_bel_between_circled_number_and_text() -> None:
    """Medical safety p.1136: a BEL appears right after the circled number (no separator or space on the page)."""
    assert strip_invisible("①\x07酸素の補助ボンベを全開にし、") == "①酸素の補助ボンベを全開にし、"


def test_strips_zero_width_and_soft_hyphen() -> None:
    assert strip_invisible("感​染­対﻿策") == "感染対策"


def test_keeps_tab_and_newline() -> None:
    assert strip_invisible("表\t列\n次行") == "表\t列\n次行"


def test_keeps_replacement_character() -> None:
    """U+FFFD marks "a glyph in the original could not be read". Dropping it hides the gap."""
    assert strip_invisible("噴霧しないでください�") == "噴霧しないでください�"


# ---------------------------------------------------------------- fix_radicals


def test_fixes_kangxi_radicals() -> None:
    """「⽇本泌尿器科学会」 (U+2F47) from 疑義解釈 K773-4."""
    assert fix_radicals("⽇本泌尿器科学会") == "日本泌尿器科学会"


def test_fixes_radicals_without_nfkc_mapping() -> None:
    """Some CJK radical supplement characters have no NFKC mapping (_RADICAL_EXTRA)."""
    assert fix_radicals("住⺠登録") == "住民登録"
    assert fix_radicals("⻑野") == "長野"


def test_fixes_kansen_standard_precautions_line() -> None:
    """Infection control 2-1: ⾎ (U+2F8E) ⽪ (U+2F6A) ⽤ (U+2F64) ⼿ (U+2F3F)."""
    assert fix_radicals("⾎液、粘膜、正常ではない⽪膚") == "血液、粘膜、正常ではない皮膚"
    assert fix_radicals("⼿指衛⽣に使⽤する") == "手指衛生に使用する"


def test_leaves_fullwidth_and_circled_numbers_alone() -> None:
    """Must be **restricted to the glyph ranges**, not whole-text NFKC (do not alter the original text)."""
    assert fix_radicals("①ＡＢ１２㎎") == "①ＡＢ１２㎎"


# ---------------------------------------------------------------- fix_symbol_bullets


def test_maps_all_known_wingdings_bullets() -> None:
    """Mapping of the 11 characters verified against the original page images for all 229 occurrences."""
    src = ""
    assert fix_symbol_bullets(src) == "●■□◆・•・▪✧➢✓"


def test_maps_bullet_at_line_head() -> None:
    assert fix_symbol_bullets(" 手指衛生を行う") == "● 手指衛生を行う"


def test_unknown_pua_is_kept_and_warned() -> None:
    """Private-use characters not in the table are **kept and warned** (not guessed, not dropped)."""
    seen: list[str] = []
    out = fix_symbol_bullets("前後", warn=seen.append)
    assert out == "前後"
    assert seen == [""]


def test_no_warning_when_all_known() -> None:
    seen: list[str] = []
    fix_symbol_bullets("", warn=seen.append)
    assert seen == []


# ---------------------------------------------------------------- despace_cjk


def test_despaces_justified_line() -> None:
    """Real example from medical safety 02_咬合系歯科.md:230."""
    assert despace_cjk("①抑 制 具 の使 用") == "①抑制具の使用"


def test_despaces_fully_split_line() -> None:
    assert despace_cjk("歯 科 診 療 時 特 有 の 注 意 点") == "歯科診療時特有の注意点"


def test_keeps_meaningful_space_after_latin() -> None:
    """「A 型 ボツリヌス」: only one spaced pair, so untouched."""
    assert despace_cjk("A 型 ボツリヌス毒素製剤を投与する") == "A 型 ボツリヌス毒素製剤を投与する"


def test_keeps_space_around_numbers() -> None:
    """Spaces next to digits/Latin letters are out of scope (not between CJK).

    Even when the whole line is judged justified, the spaces around numbers remain.
    """
    assert despace_cjk("令和 8 年 3 月 1 日") == "令和 8 年 3 月 1 日"
    assert despace_cjk("平 成 ２６年 ２月 作 成") == "平成 ２６年 ２月作成"


def test_keeps_table_columns() -> None:
    """Column-alignment runs of 2+ spaces are out of scope: the space is not flanked by CJK."""
    line = "部署名      担当者      内線番号"
    assert despace_cjk(line) == line


def test_keeps_line_below_ratio_threshold() -> None:
    """Lines listing words with single spaces are untouched (signature field of medical safety 05_血管外漏出…, ratio 0.21)."""
    line = "糖尿病・内分泌内科 医師 中村 昭伸"
    assert despace_cjk(line) == line


def test_despaces_line_diluted_by_latin_and_digits() -> None:
    """Real example with ratio 0.44 (medical safety 05_血管外漏出…:6). Missed under the 0.5 threshold."""
    src = "２）周 囲 の皮 膚 に薬 液 が漏 れないように吸 引 しながら抜 針 する。"
    assert despace_cjk(src) == "２）周囲の皮膚に薬液が漏れないように吸引しながら抜針する。"


def test_ratio_threshold_is_pinned() -> None:
    """The threshold was set to 0.4 from measurements on 3 corpora (neurology / in-hospital manuals / dermatology).

    Lower reaches committee rosters and reference author names (max 0.375); higher misses
    justified lines with ratio 0.4-0.5 (neurology 11 / medical safety 196 / dermatology 2).
    Re-measure before moving it.
    """
    assert textfix.DESPACE_MIN_RATIO == 0.4
    assert textfix.DESPACE_MIN_PAIRS == 3


def test_keeps_line_below_pair_threshold() -> None:
    line = "同意 取得"
    assert despace_cjk(line) == line


def test_does_not_touch_double_space_runs_inside_split_line() -> None:
    """Even on a spaced line, runs of 2+ spaces (column alignment) are kept."""
    assert despace_cjk("抑 制 具 の 使 用  記 録 簿") == "抑制具の使用  記録簿"


def test_text_helper_keeps_line_structure() -> None:
    src = "①抑 制 具 の使 用\n令和 8 年 3 月\n\n担 当 医 と 充 分 な 連 絡"
    assert despace_cjk_text(src) == "①抑制具の使用\n令和 8 年 3 月\n\n担当医と充分な連絡"


def test_despaces_around_middle_dot() -> None:
    """The middle dot counts as CJK too (heading in infection control 8-8). Otherwise gaps remain around the dot."""
    src = "ク ロ ス ト リ ジ オ イ デ ス ・ デ ィ フ ィ シ ル"
    assert despace_cjk(src) == "クロストリジオイデス・ディフィシル"


# -----------------------------------------------------------------------------
# A single stray space right after a line-initial circled number (medical safety 03_麻酔器の始業点検)
# -----------------------------------------------------------------------------


def test_despaces_stray_space_after_circled_number() -> None:
    """Only one space in the line, so the threshold is not reached (real example from medical safety p.1136).

    Raw pdftotext output is `①<BEL>酸 素の…`; strip_invisible drops the BEL.
    The original page p.1136 has no space (checked by eye).
    """
    src = "①酸 素の補助ボンベを全開にし、圧を確認する。酸素ボンベは充填時最高 14710 kPa（150"
    want = "①酸素の補助ボンベを全開にし、圧を確認する。酸素ボンベは充填時最高 14710 kPa（150"
    assert despace_cjk(src) == want
    assert despace_cjk("④酸 素を正しく流す。") == "④酸素を正しく流す。"
    assert despace_cjk("⑧余 剰麻酔ガス排除装置に接続し、") == "⑧余剰麻酔ガス排除装置に接続し、"


def test_circled_rule_is_pinned_to_the_head_of_the_line() -> None:
    """Off by one position and it does not apply: why the rule is pinned to "line-initial circled number + CJK + space + CJK"."""
    assert despace_cjk("①酸素 の補助ボンベ") == "①酸素 の補助ボンベ"  # space one character later
    assert despace_cjk("説明 ①酸 素の補助ボンベ") == "説明 ①酸 素の補助ボンベ"  # not line-initial


def test_circled_rule_keeps_space_directly_after_the_number() -> None:
    """A layout like 「① 酸素」 with a space right after the circled number is out of scope: the left side is not CJK."""
    assert despace_cjk("① 酸素の補助ボンベ") == "① 酸素の補助ボンベ"


def test_circled_rule_removes_only_one_space() -> None:
    """The narrow rule hits only the one at the head of the line. The rest stays under the threshold rule."""
    assert despace_cjk("①酸 素の補 助ボンベ") == "①酸素の補 助ボンベ"


def test_circled_rule_does_not_change_lines_the_threshold_handles() -> None:
    """Lines the threshold handles give the same result with the narrow rule added."""
    assert despace_cjk("①抑 制 具 の 使 用") == "①抑制具の使用"


# ---------------------------------------------------------------- safe_segment


def test_safe_segment_spaces_become_single_underscore() -> None:
    """Real data: infection control `02_14-2 針刺し…`, phone directory `02_HCU NC`, radiology `02_医科X 線検査・MRI 検査…`."""
    assert safe_segment("02_14-2 針刺し・切創及び皮膚・粘膜曝露時の対応") == "02_14-2_針刺し・切創及び皮膚・粘膜曝露時の対応"
    assert safe_segment("02_HCU NC") == "02_HCU_NC"
    assert safe_segment("02_医科X 線検査・MRI 検査の安全管理対策") == "02_医科X_線検査・MRI_検査の安全管理対策"
    assert safe_segment("a  \u3000 b") == "a_b"  # a run becomes one
    assert safe_segment("a _ b") == "a_b"  # collapse around an existing _ (never produce `__`)
    assert safe_segment(" a ") == "a"


def test_safe_segment_drops_space_next_to_cjk_punctuation() -> None:
    """Whitespace next to full-width punctuation is removed, not turned into `_` (DPC `（…。）　頸部`, medical safety `診療記録 ・ 看護記録`)."""
    assert safe_segment("070341_脊柱管狭窄（脊椎症を含む。）\u3000頸部") == "070341_脊柱管狭窄（脊椎症を含む。）頸部"
    assert safe_segment("02_診療記録 ・ 看護記録について") == "02_診療記録・看護記録について"


def test_safe_segment_hostile_ascii_to_fullwidth() -> None:
    """FAQ `065_[IIC_WEB]…` / `005_…「確定&臨時…`. `( ) . -` are kept."""
    assert safe_segment("065_[IIC_WEB]というボタン") == "065_［IIC_WEB］というボタン"
    assert safe_segment("005_確定&臨時") == "005_確定＆臨時"
    assert safe_segment('#?%+"<>\\|^`{}') == "＃？％＋＂＜＞＼｜＾｀｛｝"
    assert safe_segment("15_10-14(2) 学生等の COVID-19 対応") == "15_10-14(2)_学生等の_COVID-19_対応"
    assert safe_segment("RECIST1.1") == "RECIST1.1"


def test_safe_segment_output_passes_build_lint() -> None:
    """Must fix the same set as the lint (`PATH_HOSTILE`) in cage/cage/build.py."""
    from cage import build

    assert textfix.PATH_HOSTILE_ASCII == build.PATH_HOSTILE_ASCII
    raw = "x " + build.PATH_HOSTILE_ASCII + "\u3000y\tz"
    assert build.unsafe_paths(["/" + safe_segment(raw)]) == []


# ---------------------------------------------------------------- promote_toc_headings

_TOC_DOC = """【p.1】

１４－２．針刺し・切創及び皮膚・粘膜曝露時の対応

目次
Ⅰ．検査の体制 .......................................................... 3
Ⅱ．曝露部位の洗浄法 .................................................... 3
Ⅲ．カルバペネム耐性腸内細菌目細菌(CRE)が入院患者から新規検出された場合の連
絡・報告 .................................................................. 4

【p.3】

Ⅰ．検査の体制
  １．検査・輸血部では HBV，HCV，HIV に関する緊急検査の要請に対しては 365 日，24 時
    間体制で対応する。
  ２．検体検査室で対応する
Ⅱ．曝露部位の洗浄法
       １．注射針，メスなどで刺傷，切傷を受けた場合
Ⅲ．カルバペネム耐性腸内細菌目細菌(CRE)が入院患者から新規検出された場合の連
  絡・報告
   「サーベイランス」の項目を参照のこと。"""


def test_promote_toc_headings_only_toc_entries() -> None:
    """Infection control 14-2 shape: only roman-numeral sections in the TOC become ##. 「１．」 not in the TOC stays a list item."""
    out, promoted = promote_toc_headings(_TOC_DOC)
    assert promoted == [
        "## Ⅰ．検査の体制",
        "## Ⅱ．曝露部位の洗浄法",
        "## Ⅲ．カルバペネム耐性腸内細菌目細菌(CRE)が入院患者から新規検出された場合の連絡・報告",
    ]
    assert "  １．検査・輸血部では" in out  # list items untouched
    assert "\n## Ⅱ．曝露部位の洗浄法\n\n１．注射針" in out  # deep indent right after is dropped (no indented code)
    # TOC lines themselves are not promoted
    assert "Ⅰ．検査の体制 ......" in out


def test_promote_toc_headings_block_toc_and_levels() -> None:
    """Medical safety 「麻酔科 病棟・外来編」 shape: numbered lines right after 「目 次」 (no leaders).
    Roman = ##, arabic = ###. If the same 「1．診察」 is in 2 sections, the 2nd gets its parent appended."""
    body = "\n".join([
        "目 次",
        "",
        "Ⅰ．外来",
        "1．診察",
        "2．処方",
        "Ⅱ．病棟",
        "1．診察",
        "",
        "【p.2】",
        "Ⅰ．外来",
        "１．診察",
        "本文",
        "２．処方",
        "２．注射薬採用の変更時は、",  # already matched in TOC order → stays a list item
        "Ⅱ．病棟",
        "１．診察",
    ])
    out, promoted = promote_toc_headings(body)
    assert promoted == ["## Ⅰ．外来", "### １．診察", "### ２．処方", "## Ⅱ．病棟", "### １．診察 — Ⅱ．病棟"]
    assert "\n２．注射薬採用の変更時は、\n" in out


def test_promote_toc_headings_without_toc_is_noop() -> None:
    body = "Ⅰ．はじめに\n本文\n１．発生日時：〔　年　月　日〕\n"
    assert promote_toc_headings(body) == (body, [])


def test_promote_toc_headings_trailing_page_number_in_block_toc() -> None:
    """Infection control 8-5: entries under 「目次」 have no leaders, only a page number at the right edge."""
    body = "目次\nⅠ．判定基準                    3\nⅡ．耐性機序                    3\n\n【p.3】\nⅠ．判定基準\n本文\nⅡ．耐性機序\n"
    _out, promoted = promote_toc_headings(body)
    assert promoted == ["## Ⅰ．判定基準", "## Ⅱ．耐性機序"]


# ---------------------------------------------------------------- static_key


def test_static_key_is_short_deterministic_and_ignores_width_and_spacing() -> None:
    """Forms of infection control 14-2. The delivered-name key derives from the form name and ignores width/spacing variations."""
    k = static_key("未登録患者事前登録（削除）申込書")
    assert k == static_key("未登録患者事前登録(削除)申込書")  # NFKC
    assert k == static_key(" 未登録患者事前登録（削除）申込書\n")  # surrounding whitespace
    assert static_key("あなたを守る 手洗いとうがい") == static_key("あなたを守る\u3000\u3000手洗いとうがい")
    assert len(k) == 4 and STATIC_NAME.fullmatch(k)
    assert k != static_key("採血検査承諾書")
    assert len(static_key("x", 6)) == 6
