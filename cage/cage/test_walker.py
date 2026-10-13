"""Smoke tests for walker on the actual data/doc/ directory."""

from __future__ import annotations

from pathlib import Path

import pytest

from cage.walker import (
    GREP_MAX_LIMIT,
    Node,
    WalkerError,
    grep_subtree,
    grep_subtree_loose,
    isolate_thematic_breaks,
    join_path,
    navigate,
    segment_query,
    serialize,
    split_path,
    walk,
)

DATA_ROOT = Path(__file__).resolve().parent.parent / "data" / "doc"


def test_walks_without_errors() -> None:
    tree = walk(DATA_ROOT)
    assert tree.path == "/"
    assert tree.title == "知識ベース"


def test_root_has_guideline_category() -> None:
    """Directly under root there is one `/診療ガイドライン` (guidelines are reached via the category hierarchy)."""
    tree = walk(DATA_ROOT)
    section_paths = [c.path for c in tree.children.values()]
    assert "/診療ガイドライン" in section_paths


def test_guideline_category_lists_sections_flat() -> None:
    """Directly under `/診療ガイドライン` is a flat set of `<topic><edition>_<subcategory>` sections."""
    tree = walk(DATA_ROOT)
    cat = navigate(tree, "/診療ガイドライン")
    assert cat is not None
    section_paths = sorted(c.path for c in cat.children.values())
    assert "/診療ガイドライン/肺癌2025_小細胞癌" in section_paths
    assert "/診療ガイドライン/肺癌2025_非小細胞癌" in section_paths
    assert "/診療ガイドライン/中皮腫2025_治療" in section_paths


def test_section_node_has_merged_meta() -> None:
    """A section inherits the domain meta and keeps the guideline name."""
    tree = walk(DATA_ROOT)
    sec = navigate(tree, "/診療ガイドライン/肺癌2025_小細胞癌")
    assert sec is not None
    assert "肺癌診療ガイドライン" in sec.meta.get("guideline", "")
    assert sec.meta.get("publisher") == "日本肺癌学会"


def test_section_lists_iv_stage_cqs() -> None:
    tree = walk(DATA_ROOT)
    section = navigate(tree, "/診療ガイドライン/肺癌2025_非小細胞癌")
    assert section is not None
    cq_keys = sorted(section.children.keys())
    assert "CQ47" in cq_keys
    assert all(c.startswith("CQ") for c in cq_keys)
    assert len(cq_keys) >= 8


def test_leaf_has_body() -> None:
    tree = walk(DATA_ROOT)
    cq46 = navigate(tree, "/診療ガイドライン/肺癌2025_非小細胞癌/CQ47")
    assert cq46 is not None
    assert cq46.path == "/診療ガイドライン/肺癌2025_非小細胞癌/CQ47"
    assert "FLAURA" in cq46.body
    assert "[1]" in cq46.body
    assert cq46.children == {}


def test_navigate_missing_returns_none() -> None:
    tree = walk(DATA_ROOT)
    assert navigate(tree, "/存在しないセクション") is None
    assert navigate(tree, "/診療ガイドライン/肺癌2025_非小細胞癌/typoCQ") is None


def test_serialize_one_layer_only() -> None:
    """serialize includes only direct children; children entries do not nest."""
    tree = walk(DATA_ROOT)
    section = navigate(tree, "/診療ガイドライン/肺癌2025_非小細胞癌")
    assert section is not None
    out = serialize(section)
    assert out["path"] == "/診療ガイドライン/肺癌2025_非小細胞癌"
    assert len(out["children"]) >= 8
    # Each child has only path/title/body_chars/child_count; no children field.
    for c in out["children"]:
        assert set(c.keys()) == {"path", "title", "body_chars", "child_count"}


def test_serialize_top_has_meta_children_dont() -> None:
    """meta is only on the looked-up node itself, not on children."""
    tree = walk(DATA_ROOT)
    section = navigate(tree, "/診療ガイドライン/肺癌2025_非小細胞癌")
    assert section is not None
    out = serialize(section)
    assert "meta" in out
    for c in out["children"]:
        assert "meta" not in c


def test_serialize_no_body() -> None:
    """body is never included in serialize output (only the length via body_chars)."""
    tree = walk(DATA_ROOT)
    cq46 = navigate(tree, "/診療ガイドライン/肺癌2025_非小細胞癌/CQ47")
    assert cq46 is not None
    out = serialize(cq46)
    assert "body" not in out
    assert "body_markdown" not in out
    assert out["body_chars"] > 0


def test_serialize_body_chars_signals_readability() -> None:
    """`body_chars` signals "how many chars read_doc would return" (0 = no body).

    - root `_index.yaml` → no body → 0
    - section `_index.md` → has body → > 0
    - CQ file → has body → > 0 (equals len(body))

    Always present on the node itself and on every children entry.
    """
    tree = walk(DATA_ROOT)
    assert serialize(tree)["body_chars"] == 0  # data/doc/_index.yaml
    section = navigate(tree, "/診療ガイドライン/肺癌2025_非小細胞癌")
    assert section is not None
    assert serialize(section)["body_chars"] > 0  # _index.md
    leaf = navigate(tree, "/診療ガイドライン/肺癌2025_非小細胞癌/CQ47")
    assert leaf is not None
    assert serialize(leaf)["body_chars"] == len(leaf.body) > 0
    for c in serialize(section)["children"]:
        assert c["body_chars"] > 0  # every CQ leaf has a body


def test_split_path_roundtrip() -> None:
    cases = ["/", "/foo", "/foo/bar"]
    for c in cases:
        assert join_path(split_path(c)) == c
    # `""` is treated the same as root
    assert split_path("") == []


def test_isolate_thematic_breaks_pads_setext_underline() -> None:
    # A '---' right after a text line with no blank line triggers a setext heading.
    # Isolate it with blank lines on both sides.
    out = isolate_thematic_breaks("推奨: 強く推奨する\n---\n解説: PFS HR 0.52")
    assert out == "推奨: 強く推奨する\n\n---\n\n解説: PFS HR 0.52"


def test_isolate_thematic_breaks_handles_equals_underline() -> None:
    out = isolate_thematic_breaks("見出し\n===\n本文")
    assert out == "見出し\n\n===\n\n本文"


def test_isolate_thematic_breaks_noop_when_already_separated() -> None:
    # Already surrounded by blank lines: do not add more (no lstrip/strip needed; idempotent).
    src = "本文\n\n---\n\n次"
    assert isolate_thematic_breaks(src) == src
    assert isolate_thematic_breaks(isolate_thematic_breaks(src)) == src


def test_isolate_thematic_breaks_keeps_headings_and_lists() -> None:
    # '##' headings and lists ('- item') are not setext underlines; leave them alone.
    src = "## 推奨\n\n- 項目A\n- 項目B"
    assert isolate_thematic_breaks(src) == src


def test_isolate_thematic_breaks_skips_inside_fence() -> None:
    # '---' inside a code fence is code; do not touch it.
    src = "```\na\n---\nb\n```"
    assert isolate_thematic_breaks(src) == src


# ---------------------------------------------------------------------------
# child schema (`fields:`) validation — broken data fails startup (WalkerError)
# ---------------------------------------------------------------------------

_SCHEMA_INDEX = """---
title: セクション
fields:
  source?: str
  retrieved_at?: date
---
セクション本文
"""


def _make_tree(root: Path, index_md: str, leaf_md: str) -> None:
    """Minimal tree: root/sec/{_index.md, leaf.md}."""
    sec = root / "sec"
    sec.mkdir(parents=True)
    (sec / "_index.md").write_text(index_md, encoding="utf-8")
    (sec / "leaf.md").write_text(leaf_md, encoding="utf-8")


def test_schema_accepts_conforming_leaf(tmp_path: Path) -> None:
    _make_tree(
        tmp_path,
        _SCHEMA_INDEX,
        "---\ntitle: リーフ\nsource: http://x\nretrieved_at: '2026-01-01'\n---\n本文\n",
    )
    tree = walk(tmp_path)
    leaf = navigate(tree, "/sec/leaf")
    assert leaf is not None
    # source / retrieved_at are ? hidden → validated but not in meta (= hidden from /doc/list)
    assert leaf.meta == {}


def test_schema_list_on_off_projection(tmp_path: Path) -> None:
    """Unmarked fields stay in meta; only `?` fields are dropped (on/off checked on the same node)."""
    index = (
        "---\n"
        "title: セクション\n"
        "fields:\n"
        "  guideline: str\n"  # unmarked → shown in list
        "  source?: str\n"  # ? → hidden from list
        "---\n本文\n"
    )
    _make_tree(
        tmp_path,
        index,
        "---\ntitle: リーフ\nguideline: GL名\nsource: http://x\n---\n本文\n",
    )
    leaf = navigate(walk(tmp_path), "/sec/leaf")
    assert leaf is not None
    assert leaf.meta == {"guideline": "GL名"}  # guideline stays / source is dropped


def test_schema_rejects_unexpected_field(tmp_path: Path) -> None:
    """closed: an undeclared key fails startup."""
    _make_tree(
        tmp_path,
        _SCHEMA_INDEX,
        "---\ntitle: L\nsource: x\nretrieved_at: '2026-01-01'\nbogus: 1\n---\nb\n",
    )
    with pytest.raises(WalkerError, match="unexpected"):
        walk(tmp_path)


def test_schema_rejects_missing_field(tmp_path: Path) -> None:
    """All required: a missing declared key fails startup (? is visibility only, not optional)."""
    _make_tree(tmp_path, _SCHEMA_INDEX, "---\ntitle: L\nsource: x\n---\nb\n")
    with pytest.raises(WalkerError, match="missing required"):
        walk(tmp_path)


def test_schema_rejects_wrong_type(tmp_path: Path) -> None:
    _make_tree(
        tmp_path,
        _SCHEMA_INDEX,
        "---\ntitle: L\nsource: x\nretrieved_at: not-a-date\n---\nb\n",
    )
    with pytest.raises(WalkerError, match="must be date"):
        walk(tmp_path)


def test_absent_fields_means_title_only(tmp_path: Path) -> None:
    """No `fields` = children have title only (closed). Any non-title key fails startup."""
    _make_tree(
        tmp_path,
        "---\ntitle: S\n---\n本文\n",  # no fields declaration
        "---\ntitle: L\nextra: 1\n---\nb\n",
    )
    with pytest.raises(WalkerError, match="unexpected"):
        walk(tmp_path)


def test_bad_fields_declaration_rejected(tmp_path: Path) -> None:
    """An invalid `fields` declaration itself (unknown type) fails startup."""
    _make_tree(
        tmp_path,
        "---\ntitle: S\nfields:\n  x: bogustype\n---\n本文\n",
        "---\ntitle: L\nx: 1\n---\nb\n",
    )
    with pytest.raises(WalkerError, match="unknown type"):
        walk(tmp_path)


# ---------------------------------------------------------------------------
# loose grep (word segmentation + BM25)
# ---------------------------------------------------------------------------


def test_segment_query_splits_on_script_boundary() -> None:
    """Without a dictionary, script changes (kanji/kana/katakana/alnum) are used as word boundaries."""
    assert segment_query("転倒 報告書") == ["転倒", "報告書"]
    assert segment_query("オシメルチニブの副作用") == ["オシメルチニブ", "副作用"]
    assert segment_query("抗菌薬の適正使用") == ["抗菌薬", "適正使用"]


def test_segment_query_drops_hiragana_only_terms() -> None:
    """Hiragana-only terms are particles / inflection endings; they pollute matched_terms, so drop them up front."""
    assert segment_query("患者さんが転んでしまった場合の報告") == [
        "患者",
        "転",
        "場合",
        "報告",
    ]
    assert segment_query("けがをしてしまった") == []


def test_segment_query_keeps_identifiers_intact() -> None:
    """Letters and digits are not split: "CQ47" as "cq"+"47" would lose the identifier's rarity."""
    assert segment_query("CQ47 の推奨") == ["cq47", "推奨"]
    assert segment_query("ＥＧＦＲ陽性") == ["egfr", "陽性"]  # NFKC also absorbs fullwidth


def test_segment_query_dedupes_preserving_order() -> None:
    assert segment_query("転倒 転倒 報告") == ["転倒", "報告"]


def test_segment_query_keeps_long_vowel_marks_with_the_kana_run() -> None:
    """The long vowel mark (ー) belongs to the preceding kana word. Medical vocabulary is full of them, so this rule is key."""
    assert segment_query("カテーテル関連血流感染") == ["カテーテル", "関連血流感染"]
    assert segment_query("サーベイランスのデータ") == ["サーベイランス", "データ"]
    assert segment_query("ドレーン管理") == ["ドレーン", "管理"]
    # hiragana + long vowel mark also forms one word, which is hiragana-only and dropped
    assert segment_query("あーそうか") == []


def test_segment_query_handles_other_scripts_and_halfwidth_kana() -> None:
    """Unknown scripts are grouped into one word. Halfwidth kana are normalized to fullwidth by NFKC."""
    assert segment_query("한글テスト") == ["한글", "テスト"]
    assert segment_query("ﾊﾝｶｸｶﾅの語") == ["ハンカクカナ", "語"]


def _loose_tree(root: Path, docs: dict[str, str]) -> None:
    """Build a flat tree from {name: body}."""
    sec = root / "sec"
    sec.mkdir(parents=True)
    (sec / "_index.md").write_text("---\ntitle: セクション\n---\n", encoding="utf-8")
    for name, body in docs.items():
        (sec / f"{name}.md").write_text(
            f"---\ntitle: {name}\n---\n{body}\n", encoding="utf-8"
        )


def test_loose_does_not_require_the_rarest_term(tmp_path: Path) -> None:
    """The rarest term is not required. Rarity order is not importance order.

    Observed regression: for "MRSAの接触予防策", requiring "接触予防策" (df=5) dropped the
    MRSA node of the per-pathogen manual (104 occurrences of MRSA) just for lacking that literal.
    """
    _loose_tree(
        tmp_path,
        {
            "希少語だけ": "接触予防策",
            "主題そのもの": "mrsa " * 50,
            "無関係": "全く別の話",
        },
    )
    tree = walk(tmp_path)
    matches, total, _, _ = grep_subtree_loose(tree, ["mrsa", "接触予防策"], limit=50)
    paths = {m["path"] for m in matches}
    assert total == 2
    assert "/sec/主題そのもの" in paths, "the main hit must not be dropped merely for lacking the rare term"
    assert "/sec/希少語だけ" in paths


def test_loose_prefers_documents_matching_more_terms(tmp_path: Path) -> None:
    """Coordination factor. Plain BM25 just sums, so without it a single-term mass hit wins.

    Observed regression: for "バンコマイシンのTDM", a TDM-only document ranked above one containing both.
    """
    _loose_tree(
        tmp_path,
        {
            "両方": "バンコマイシン tdm",
            "片方だけ大量": "tdm " * 60,
        },
    )
    tree = walk(tmp_path)
    matches, _, _, _ = grep_subtree_loose(tree, ["バンコマイシン", "tdm"], limit=50)
    assert matches[0]["path"] == "/sec/両方"
    assert matches[0]["matched_terms"] == ["バンコマイシン", "tdm"]


def test_loose_drops_terms_that_appear_almost_everywhere(tmp_path: Path) -> None:
    """Low-information terms are removed from both the hit condition and the score, and reported as ignored.

    Observed regression: for "針刺し事故が起きたときの対応" more than half the score came from 事故/起/対応,
    and the main hit (title containing "針刺し") lost to per-department pages, falling to rank 8.
    """
    docs = {f"部署{i:02d}": "対応 の 手順" for i in range(60)}
    docs["本命"] = "針刺 の 対応"
    _loose_tree(tmp_path, docs)
    tree = walk(tmp_path)
    matches, total, absent, ignored = grep_subtree_loose(
        tree, ["針刺", "対応"], limit=50
    )
    assert ignored == ["対応"], "a term present in every node is excluded from the search"
    assert absent == []
    assert total == 1
    assert matches[0]["path"] == "/sec/本命"
    assert matches[0]["matched_terms"] == ["針刺"]


def test_loose_keeps_all_terms_when_subtree_is_small(tmp_path: Path) -> None:
    """With a small population 10% is a handful of docs and misfires, so no ubiquitous-term detection."""
    _loose_tree(tmp_path, {"a": "針刺 対応", "b": "対応"})
    tree = walk(tmp_path)
    _, _, _, ignored = grep_subtree_loose(tree, ["針刺", "対応"], limit=50)
    assert ignored == []


def test_loose_reports_terms_absent_from_the_corpus(tmp_path: Path) -> None:
    """Terms with zero occurrences are returned as absent (the core feedback to the model)."""
    _loose_tree(tmp_path, {"a": "転倒の報告について"})
    tree = walk(tmp_path)
    _, _, absent, _ = grep_subtree_loose(tree, ["転倒", "架空語"], limit=50)
    assert absent == ["架空語"]


def test_loose_returns_nothing_when_every_term_is_absent(tmp_path: Path) -> None:
    _loose_tree(tmp_path, {"a": "転倒の報告について"})
    tree = walk(tmp_path)
    matches, total, absent, _ = grep_subtree_loose(
        tree, ["架空語", "別の架空語"], limit=50
    )
    assert (matches, total) == ([], 0)
    assert absent == ["架空語", "別の架空語"]


def test_loose_title_hit_outranks_body_hits(tmp_path: Path) -> None:
    """Field weight (title×10) is applied on the tf side with the same value as exact."""
    _loose_tree(
        tmp_path,
        {
            "転倒": "報告",
            "その他": "転倒 報告 " * 3,
        },
    )
    tree = walk(tmp_path)
    matches, _, _, _ = grep_subtree_loose(tree, ["転倒", "報告"], limit=50)
    assert matches[0]["path"] == "/sec/転倒"
    assert matches[0]["match_count"]["title"] == 1


# ---------------------------------------------------------------------------
# field weights (title×10 / description×5 / body×1) bracketed at the boundary
# ---------------------------------------------------------------------------
#
# These values are specified in the README. Checking monotonic decrease alone would still pass
# if 10 were lowered to 3, so the value itself is pinned by bracketing the boundary from both
# sides: "1 title hit beats 9 body hits and loses to 11 body hits".
# Node names are chosen so that **a tie breaks the expected order** (ties sort by path
# ascending, so the test fails the moment a drifted weight produces a tie).


def _weighted_tree(root: Path, docs: dict[str, str], *, with_desc: bool = False) -> None:
    sec = root / "sec"
    sec.mkdir(parents=True)
    index = "---\ntitle: セクション\n"
    if with_desc:
        index += "fields:\n  description: str\n"
    index += "---\n"
    (sec / "_index.md").write_text(index, encoding="utf-8")
    for name, spec in docs.items():
        (sec / f"{name}.md").write_text(spec, encoding="utf-8")


def _leaf(title: str, body: str, description: str | None = None) -> str:
    fm = f"---\ntitle: {title}\n"
    if description is not None:
        fm += f"description: {description}\n"
    return fm + "---\n" + body + "\n"


def test_title_weight_beats_nine_body_hits(tmp_path: Path) -> None:
    """Lower bound of title×10. With a weight of 9 or less, 9 body hits catch up."""
    _weighted_tree(
        tmp_path,
        {
            # on a tie, a_ comes first by path order and the assert fails
            "a_body9": _leaf("無関係", "X " * 9),
            "z_title": _leaf("X の話", "本文にヒットなし"),
        },
    )
    tree = walk(tmp_path)
    matches, _ = grep_subtree(tree, "X", limit=10)
    assert [m["path"] for m in matches] == ["/sec/z_title", "/sec/a_body9"]


def test_title_weight_loses_to_eleven_body_hits(tmp_path: Path) -> None:
    """Upper bound of title×10. With a weight of 11 or more, it overtakes 11 body hits."""
    _weighted_tree(
        tmp_path,
        {
            "a_title": _leaf("X の話", "本文にヒットなし"),
            "z_body11": _leaf("無関係", "X " * 11),
        },
    )
    tree = walk(tmp_path)
    matches, _ = grep_subtree(tree, "X", limit=10)
    assert [m["path"] for m in matches] == ["/sec/z_body11", "/sec/a_title"]


def test_description_weight_is_bracketed_at_five(tmp_path: Path) -> None:
    """description×5 bracketed from both sides (beats body 4, loses to body 6)."""
    _weighted_tree(
        tmp_path,
        {
            "a_body4": _leaf("無関係", "X " * 4, description="関係なし"),
            "z_desc": _leaf("無関係", "本文にヒットなし", description="X の説明"),
        },
        with_desc=True,
    )
    tree = walk(tmp_path)
    matches, _ = grep_subtree(tree, "X", limit=10)
    assert [m["path"] for m in matches] == ["/sec/z_desc", "/sec/a_body4"]

    root2 = tmp_path / "upper"
    root2.mkdir()
    _weighted_tree(
        root2,
        {
            "a_desc": _leaf("無関係", "本文にヒットなし", description="X の説明"),
            "z_body6": _leaf("無関係", "X " * 6, description="関係なし"),
        },
        with_desc=True,
    )
    tree2 = walk(root2)
    matches2, _ = grep_subtree(tree2, "X", limit=10)
    assert [m["path"] for m in matches2] == ["/sec/z_body6", "/sec/a_desc"]


# ---------------------------------------------------------------------------
# loose details (rescue branch / meaning of match_count / snippet selection)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# The core of issue #6: the "ubiquitous term" decision must not depend on corpus size
# ---------------------------------------------------------------------------
#
# The old rule compared df against **the number of searched documents**, so merely adding a
# corpus unrelated to the term moved it in and out of the search and the main hit's rank jumped
# (observed: adding 305 FAQ entries dropped "事故" from 13.3% to 8.9%, and the main hit 14-2 for
# "針刺し事故…" went from rank 1 to rank 10).
# This is a **design problem, not a test problem**, so instead of asserting either side of the
# threshold directly, we assert "adding or removing a whole section does not move the rank".

CORPUS_STABILITY_CASES = [
    (
        "針刺し事故が起きたときの対応",
        "/院内マニュアル/感染対策マニュアル/14_職業感染対策"
        "/02_14-2_針刺し・切創及び皮膚・粘膜曝露時の対応",
    ),
    (
        "転倒 報告書",
        "/院内マニュアル/医療安全管理マニュアル/03_共通マニュアル/06_その他の項目"
        "/01_転落・転倒防止対策",
    ),
    (
        "新型コロナウイルス感染症の対応",
        "/院内マニュアル/感染対策マニュアル/10_病原体別予防策（ウイルス）"
        "/14_10-14_新型コロナウイルス感染症",
    ),
]

# Observed spread: at most 5 over 15 design queries / 0 over 21 validation queries (2026-09-04,
# 4 corpus sizes of 930 / 1,304 / 2,028 / 3,895 nodes, mixed, including 2,590 診療報酬 (fee schedule) nodes).
# Under the old rule the same measurement gave at most 19.
CORPUS_STABILITY_TOLERANCE = 3


def _loose_rank(tree: Node, query: str, expected_path: str) -> int | None:
    matches, _, _, _ = grep_subtree_loose(
        tree, segment_query(query), limit=GREP_MAX_LIMIT * 100
    )
    for i, m in enumerate(matches, 1):
        if m["path"] == expected_path:
            return i
    return None


def test_loose_rank_does_not_move_when_unrelated_sections_are_added_or_removed() -> None:
    """The main hit's rank does not move with the presence/absence of unrelated sections (issue #6).

    Either direction works, so we measure by **dropping one section from the real data**
    (adding data cannot be reproduced, but add/remove is symmetric, so it checks the same thing).
    Only top-level sections not containing the main hit are dropped.
    """
    for query, expected in CORPUS_STABILITY_CASES:
        base = walk(DATA_ROOT)
        top = split_path(expected)[0]
        full_rank = _loose_rank(base, query, expected)
        assert full_rank is not None, f"{query}: expected hit {expected} does not appear in loose results"
        for section in list(base.children):
            if section == top:
                continue
            reduced = walk(DATA_ROOT)
            del reduced.children[section]
            rank = _loose_rank(reduced, query, expected)
            assert rank is not None, (
                f"{query}: removing /{section} made the expected hit vanish from the results "
                f"(rank {full_rank} with the full corpus)"
            )
            assert abs(rank - full_rank) <= CORPUS_STABILITY_TOLERANCE, (
                f"{query}: removing /{section} moves the expected hit's rank {full_rank} → {rank}. "
                "The ubiquitous-term decision depends on corpus size (issue #6)"
            )


def _mixed_corpus(root: Path, *, with_short_corpus: bool) -> None:
    """A long-document corpus + (optionally) a separate corpus of short bodies.

    Minimal reproduction of what happened on real data (adding 2,590 診療報酬 (fee schedule) nodes dropped
    the overall average from 2,961 to 2,252 chars). **The added side contains none of the query
    terms**, so the only difference is purely "the average document length moved".
    """
    manual = root / "manual"
    manual.mkdir(parents=True)
    (manual / "_index.md").write_text("---\ntitle: マニュアル\n---\n", encoding="utf-8")
    # expected hit: has both terms but is long.
    (manual / "a_answer.md").write_text(
        _leaf("取扱手順", ("解説文 " * 900) + "特殊薬剤 " + ("解説文 " * 900) + "測定法"),
        encoding="utf-8",
    )
    # decoy: has only the rarer term, many times, but is short.
    (manual / "b_decoy.md").write_text(
        _leaf("別の頁", "測定法 " * 12), encoding="utf-8"
    )
    # population (per-group averages need at least LOOSE_COMMON_MIN_DOCS = 50 docs).
    for i in range(58):
        (manual / f"c_filler{i:02d}.md").write_text(
            _leaf(f"填め物{i:02d}", "無関係な本文 " * 300), encoding="utf-8"
        )
    if not with_short_corpus:
        return
    short = root / "short"
    short.mkdir(parents=True)
    (short / "_index.md").write_text("---\ntitle: 短文集\n---\n", encoding="utf-8")
    for i in range(300):
        (short / f"s{i:03d}.md").write_text(
            _leaf(f"短文{i:03d}", "点数 区分 算定 " * 8), encoding="utf-8"
        )


def test_loose_rank_survives_a_short_document_corpus_being_added(
    tmp_path: Path,
) -> None:
    """Adding a short-document corpus sharing no terms does not move the expected hit's rank (issue #6).

    BM25 length normalization asks "is it long relative to the average document length (avgdl)?".
    If avgdl is taken over the whole search scope, **merely adding an unrelated corpus changes the
    penalty of existing nodes** — adding many short documents lowers avgdl, and the longer the
    expected hit, the more it is punished. Taking avgdl per group (direct children of the
    search scope) avoids this.

    `test_loose_rank_does_not_move_when_unrelated_sections_are_added_or_removed` checks the
    "remove" side on real data; this checks the **add** side on a synthetic tree.
    """
    base, mixed = tmp_path / "base", tmp_path / "mixed"
    _mixed_corpus(base, with_short_corpus=False)
    _mixed_corpus(mixed, with_short_corpus=True)
    terms = segment_query("特殊薬剤の測定法")

    ranks = {}
    for label, path in (("短文なし", base), ("短文あり", mixed)):
        matches, _, _, _ = grep_subtree_loose(walk(path), terms, limit=GREP_MAX_LIMIT)
        paths = [m["path"] for m in matches]
        assert "/manual/a_answer" in paths, f"{label}: expected hit missing from results {paths[:5]}"
        ranks[label] = paths.index("/manual/a_answer") + 1

    assert ranks["短文なし"] == ranks["短文あり"], (
        f"adding the short corpus moved the expected hit's rank {ranks['短文なし']} → {ranks['短文あり']}. "
        "The length-normalization baseline (avgdl) depends on corpus size (issue #6)"
    )
    assert ranks["短文あり"] == 1, (
        f"the expected hit with both terms is not rank 1 (rank {ranks['短文あり']}). "
        "It loses to a short page with only one term, merely for being long"
    )


def test_loose_drops_a_lone_kanji_left_over_from_okurigana(tmp_path: Path) -> None:
    """A lone kanji left over after okurigana is stripped is kept out of core and reported in ignored_terms.

    segment_query cuts at script boundaries, so "起きたときの" yields "起". It is not a content
    word, yet as the corpus grows its IDF ratio approaches 1 and it slips through the
    ubiquitous-term net (observed: 0.49 at 930 nodes → 0.50 at 3,895 nodes, crossing the 0.5
    threshold). Term length does not depend on the corpus, so drop it on that basis instead.
    """
    docs = {f"n{i:02d}": _leaf(f"n{i:02d}", "起 事案") for i in range(50)}
    for i in range(50, 60):
        docs[f"n{i:02d}"] = _leaf(f"n{i:02d}", "起 事案 針刺")
    _weighted_tree(tmp_path, docs)
    tree = walk(tmp_path)
    _, _, absent, ignored = grep_subtree_loose(tree, ["針刺", "事案", "起"], limit=5)
    assert absent == []
    assert "起" in ignored, f"lone kanji left in core: ignored={ignored}"


def test_loose_keeps_a_lone_kanji_that_is_the_most_informative_term(
    tmp_path: Path,
) -> None:
    """Even a lone kanji is kept when it is the most informative term of the query.

    A single kanji such as "癌" or "肺" can be a real content word. It is dropped only
    "when there is a more effective term" — otherwise "癌の治療" would turn into a
    search for "治療" alone.
    """
    docs = {f"n{i:02d}": _leaf(f"n{i:02d}", "治療 方針") for i in range(55)}
    for i in range(55, 60):
        docs[f"n{i:02d}"] = _leaf(f"n{i:02d}", "治療 方針 癌")
    _weighted_tree(tmp_path, docs)
    tree = walk(tmp_path)
    matches, _, _, ignored = grep_subtree_loose(tree, ["癌", "治療"], limit=5)
    assert "癌" not in ignored, f"the max-IDF lone kanji must not be dropped: {ignored}"
    assert matches, "results become empty when the max-IDF term is a lone kanji"
    assert all("癌" in m["matched_terms"] for m in matches)


def test_loose_keeps_only_the_informative_term_when_terms_differ(tmp_path: Path) -> None:
    """Even if both are common, only the more informative one is kept.

    Under the old rule (df > 10% of the population) both exceeded the threshold, core became
    empty, and it fell into the "retry with the single rarest term" rescue branch. With the IDF
    ratio the max-IDF term always stays at ratio 1.0, so **core is never empty and the rescue
    branch became unreachable** (hence removed). What is pinned here is the result — the rarer
    one survives via the normal path, not via a retry.
    """
    docs = {f"n{i:02d}": _leaf(f"n{i:02d}", "共通甲 共通乙") for i in range(55)}
    for i in range(55, 60):
        docs[f"n{i:02d}"] = _leaf(f"n{i:02d}", "共通甲 のみ")
    _weighted_tree(tmp_path, docs)
    tree = walk(tmp_path)
    matches, total, absent, ignored = grep_subtree_loose(
        tree, ["共通甲", "共通乙"], limit=5
    )
    assert ignored == ["共通甲"], "the lower-IDF term is dropped (共通甲 df=60 / 共通乙 df=55)"
    assert absent == []
    assert total == 55
    assert all(m["matched_terms"] == ["共通乙"] for m in matches)


def test_loose_keeps_equally_informative_terms_together(tmp_path: Path) -> None:
    """Equally informative terms are both kept, even if both are common.

    The old rule fell into "both df > 10% → empty core → rescue with one term". There is no
    basis for dropping either of two terms with equal df, and dropping one **changes the
    coordination factor denominator and thus the meaning of the ranking**. On a tie, keep all.
    """
    docs = {f"n{i:02d}": _leaf(f"n{i:02d}", "短語 長い複合語") for i in range(60)}
    _weighted_tree(tmp_path, docs)
    tree = walk(tmp_path)
    matches, _, _, ignored = grep_subtree_loose(tree, ["短語", "長い複合語"], limit=3)
    assert ignored == []
    assert all(m["matched_terms"] == ["短語", "長い複合語"] for m in matches)


def _idf_ratio_tree(tmp_path: Path, common_df: int, total: int = 60) -> None:
    """A tree of `total` docs with a rare term (1 doc) and a common term (df=common_df)."""
    docs = {f"n{i:02d}": _leaf(f"n{i:02d}", "ありふれ語") for i in range(common_df)}
    for i in range(common_df, total):
        docs[f"n{i:02d}"] = _leaf(f"n{i:02d}", "無関係な本文")
    docs["rare"] = _leaf("rare", "希少語")
    _weighted_tree(tmp_path, docs)


def test_loose_keeps_a_term_just_above_the_idf_ratio(tmp_path: Path) -> None:
    """A term **just above** the threshold is kept (first of the two boundary brackets).

    62 nodes / rare term df=1 vs df=9 gives an IDF ratio of 0.506. **Under the old rule
    (df > 10% of the population = 6.2) this was dropped** — a content word vanishing just for
    being queried together with a rare term was the failure of the min(df) criterion withdrawn
    on 2026-08-14 (issue #6); this is its regression check.
    """
    _idf_ratio_tree(tmp_path, common_df=9)
    tree = walk(tmp_path)
    _, _, _, ignored = grep_subtree_loose(tree, ["希少語", "ありふれ語"], limit=5)
    assert ignored == []


def test_loose_drops_a_term_just_below_the_idf_ratio(tmp_path: Path) -> None:
    """A term **just below** the threshold is dropped (second of the two boundary brackets).

    With the same 62 nodes, df=10 gives an IDF ratio of 0.479. Unless we pin that moving a
    single doc flips the decision, nobody notices when the constant changes (which is why the
    2026-08-14 revert turned no test red).
    """
    _idf_ratio_tree(tmp_path, common_df=10)
    tree = walk(tmp_path)
    _, _, _, ignored = grep_subtree_loose(tree, ["希少語", "ありふれ語"], limit=5)
    assert ignored == ["ありふれ語"]


def test_loose_match_count_sums_occurrences_across_terms(tmp_path: Path) -> None:
    """loose match_count is the **sum** of occurrences across terms; its meaning differs from exact.

    Specified in both the README and the tool docstring. Changing sum to max would not change
    the hit count, so without pinning it here it breaks silently. title may be 2 or more,
    another difference from exact.
    """
    _weighted_tree(tmp_path, {"n": _leaf("甲 と 乙", "甲 甲 甲 乙 乙 乙")})
    tree = walk(tmp_path)
    matches, _, _, _ = grep_subtree_loose(tree, ["甲", "乙"], limit=5)
    assert matches[0]["match_count"]["body"] == 6, "sum of 3 + 3"
    assert matches[0]["match_count"]["title"] == 2, "0|1 in exact, but the term count in loose"


def test_loose_snippet_shows_the_rarest_matched_term(tmp_path: Path) -> None:
    """The snippet is the first occurrence of the rarest term, showing why that node was chosen.

    In loose mode the snippet is the only body fragment the model uses to judge whether it can
    cite the node, so regressing to "the first term that hit" is a meaningful degradation.
    """
    docs = {f"other{i}": _leaf(f"other{i}", "ありふれ語 の説明") for i in range(6)}
    docs["target"] = _leaf("target", "ありふれ語 が冒頭にある。" + "詰め物。" * 20 + "希少語 が末尾。")
    _weighted_tree(tmp_path, docs)
    tree = walk(tmp_path)
    matches, _, _, _ = grep_subtree_loose(tree, ["ありふれ語", "希少語"], limit=5)
    top = next(m for m in matches if m["path"] == "/sec/target")
    assert "希少語" in top["body_first_match_snippet"]
    assert "ありふれ語" not in top["body_first_match_snippet"]


if __name__ == "__main__":
    import sys
    import traceback

    fns = [
        test_walks_without_errors,
        test_root_has_guideline_category,
        test_guideline_category_lists_sections_flat,
        test_section_node_has_merged_meta,
        test_section_lists_iv_stage_cqs,
        test_leaf_has_body,
        test_navigate_missing_returns_none,
        test_serialize_one_layer_only,
        test_serialize_top_has_meta_children_dont,
        test_serialize_no_body,
        test_serialize_body_chars_signals_readability,
        test_split_path_roundtrip,
        test_isolate_thematic_breaks_pads_setext_underline,
        test_isolate_thematic_breaks_handles_equals_underline,
        test_isolate_thematic_breaks_noop_when_already_separated,
        test_isolate_thematic_breaks_keeps_headings_and_lists,
        test_isolate_thematic_breaks_skips_inside_fence,
    ]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS: {fn.__name__}")
        except Exception:
            print(f"FAIL: {fn.__name__}")
            traceback.print_exc()
            failed += 1
    sys.exit(1 if failed else 0)
