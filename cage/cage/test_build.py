"""Tests for build.py (doc generation) and render.py (HTML rendering) on a small synthetic corpus."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cage import build, render
from cage.walker import walk

VALUES = {"HIS_FAQ_BASE": "https://his.example/his-info"}


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


@pytest.fixture
def src(tmp_path: Path) -> Path:
    root = tmp_path / "src"
    _write(root / "_index.yaml", "title: 知識ベース\n")
    _write(
        root / "マニュアル" / "_index.md",
        "---\ntitle: マニュアル\n---\n\n概要。図は {{DOC_STATIC_BASE}}/a.jpg\n",
    )
    _write(
        root / "マニュアル" / "05_転倒.md",
        "---\ntitle: 転倒・転落\n---\n\n"
        "## 転倒・転落\n\n本文\n---\n続き\n\n"
        "## 対応\n\n![図](/ext/doc-static/f.jpg)\n\n"
        "### 対応\n\n## 対応\n\n## **強調** と `code`\n\n"
        "## 50%減少\n\n"
        "原本は [FAQ]({{HIS_FAQ_BASE}}/faq.aspx) 今日は {{CURRENT_DATE}}\n\n"
        "<script>alert(1)</script>\n\n表<br>改行 <BEL>\n\n[x](javascript:alert(1))\n",
    )
    _write(root / "マニュアル" / "RECIST1.1.md", "---\ntitle: RECIST\n---\n\n# RECIST\n\n本文\n")
    _write(root / "空" / "子.md", "---\ntitle: 子\n---\n")
    return root


def _build(src: Path, tmp_path: Path, **kw) -> tuple[Path, dict]:
    out = tmp_path / "out" / "doc"
    return out, build.build(src, out, VALUES, workers=1, **kw)


# ---------------------------------------------------------------------------
# generated output (md)
# ---------------------------------------------------------------------------


def test_layout_matches_node_paths(src: Path, tmp_path: Path) -> None:
    """Output is only md / yaml in the same relative layout as the source, 1:1 with node.path. No HTML."""
    out, _m = _build(src, tmp_path)
    files = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
    assert files == [
        "_index.yaml",
        "マニュアル/05_転倒.md",
        "マニュアル/RECIST1.1.md",
        "マニュアル/_index.md",
        "空/子.md",
    ]

    def paths(n):
        yield n.path
        for c in n.children.values():
            yield from paths(c)

    assert list(paths(walk(out))) == list(paths(walk(src)))


def test_compiled_md_substitutes_and_keeps_front_matter(src: Path, tmp_path: Path) -> None:
    out, _m = _build(src, tmp_path)
    text = (out / "マニュアル" / "05_転倒.md").read_text(encoding="utf-8")
    assert text.startswith("---\ntitle: 転倒・転落\n---\n")
    assert "{{HIS_FAQ_BASE}}" not in text
    assert "(https://his.example/his-info/faq.aspx)" in text
    assert "{{CURRENT_DATE}}" in text  # nucllei variables are left alone
    idx = (out / "マニュアル" / "_index.md").read_text(encoding="utf-8")
    assert "/ext/doc-static/a.jpg" in idx  # {{DOC_STATIC_BASE}} in the source is substituted
    assert "{{DOC_STATIC_BASE}}" not in idx


def test_compiled_md_isolates_setext(src: Path, tmp_path: Path) -> None:
    out, _m = _build(src, tmp_path)
    node = walk(out).children["マニュアル"].children["05_転倒"]
    assert "本文\n\n---\n\n続き" in node.body


def test_fixed_placeholders_are_not_deploy_values(src: Path, tmp_path: Path) -> None:
    """`{{DOC_BASE}}` / `{{DOC_STATIC_BASE}}` are fixed values (walker constants), one name = one value.
    Passing them as deploy values has no effect. The old `{{PUBLIC_BASE}}` is no longer substituted (lint catches it)."""
    out = tmp_path / "abs" / "doc"
    m = build.build(
        src, out, {**VALUES, "DOC_STATIC_BASE": "http://llens-host:13303"}, workers=1
    )
    idx = (out / "マニュアル" / "_index.md").read_text(encoding="utf-8")
    assert " /ext/doc-static/a.jpg" in idx
    assert "llens-host" not in idx
    assert m["values"] == VALUES
    assert build.substitute(
        "{{DOC_BASE}}/a/b.md {{DOC_BASE}}/a/ {{DOC_STATIC_BASE}}/x {{PUBLIC_BASE}}/static/y", {}
    ) == "/ext/doc/a/b.md /ext/doc/a/ /ext/doc-static/x {{PUBLIC_BASE}}/static/y"


def test_duplicate_headings_are_warned_in_manifest(src: Path, tmp_path: Path) -> None:
    out, m = _build(src, tmp_path)
    assert m["duplicate_headings"] == {"/マニュアル/05_転倒": ["対応"]}
    on_disk = json.loads(build.manifest_path(out).read_text(encoding="utf-8"))
    assert on_disk["duplicate_headings"] == m["duplicate_headings"]
    assert on_disk["values"] == VALUES
    assert on_disk["unsafe_paths"] == []


def test_unsafe_path_segments_are_warned_not_fatal(src: Path, tmp_path: Path, caplog) -> None:
    """Segments containing whitespace or URL-hostile ASCII get a warning + manifest `unsafe_paths`. Build is not stopped."""
    _write(src / "マニュアル" / "06_院内 マニュアル.md", "---\ntitle: a\n---\n\n本文\n")
    _write(src / "マニュアル" / "07_[IIC]＆確定.md", "---\ntitle: b\n---\n\n本文\n")
    _write(src / "マニュアル" / "08_全角［ok］＆_ok.md", "---\ntitle: c\n---\n\n本文\n")
    with caplog.at_level("WARNING", logger="cage.build"):
        out, m = _build(src, tmp_path)
    assert m["unsafe_paths"] == ["/マニュアル/06_院内 マニュアル", "/マニュアル/07_[IIC]＆確定"]
    assert (out / "マニュアル" / "06_院内 マニュアル.md").is_file()  # still generated
    assert "2 node paths contain whitespace" in caplog.text
    on_disk = json.loads(build.manifest_path(out).read_text(encoding="utf-8"))
    assert on_disk["unsafe_paths"] == m["unsafe_paths"]


def test_unsafe_paths_detects_each_hostile_char() -> None:
    for ch in build.PATH_HOSTILE_ASCII + " \u3000\t":
        assert build.unsafe_paths([f"/a/x{ch}y"]) == [f"/a/x{ch}y"], repr(ch)
    # Japanese, fullwidth punctuation, `_` `-` `.` `(` `)` are fine
    assert build.unsafe_paths(["/", "/院内マニュアル/15_10-14(2)_学生等の_COVID-19_対応", "/a/［x］＆RECIST1.1"]) == []


def test_manifest_skip_and_rebuild(src: Path, tmp_path: Path) -> None:
    out, m1 = _build(src, tmp_path)
    assert m1["skipped"] is False
    (out / ".keep").write_text("")  # git-tracked .keep must survive cleanup
    out, m2 = _build(src, tmp_path)
    assert m2["skipped"] is True
    # Forcing yields identical content (deterministic) — nothing is rewritten
    out, m3 = _build(src, tmp_path, force=True)
    assert m3["skipped"] is False and m3["written"] == 0
    # A changed deploy value triggers a rebuild
    m4 = build.build(src, out, {**VALUES, "HIS_FAQ_BASE": "http://other"}, workers=1)
    assert m4["skipped"] is False and m4["written"] == 1
    # A changed source triggers a rebuild; output of removed nodes is deleted
    (src / "空" / "子.md").unlink()
    (src / "空").rmdir()
    out, m5 = _build(src, tmp_path)
    assert m5["skipped"] is False and m5["removed"] == 1
    assert not (out / "空").exists()
    assert out.is_dir() and (out / ".keep").is_file()  # the output root is not recreated


def test_internal_links_normalizes_targets() -> None:
    text = (
        "[原本](/ext/doc-static/k/docs/14-2.pdf#page=3) "
        "![図](/ext/doc-static/k/figs/a%20b.jpg) "
        "係数は /ext/doc-static/dpc/x.xlsx。 "
        "**/ext/doc-static/y.csv**, "
        "[節](/ext/doc/院内/05_転倒.md) [章](/ext/doc/院内/) [章](/ext/doc/院内/_index.md) "
        "[頁](/ext/doc/院内/05_転倒#対応) "
        "書式は `/ext/doc/<path>`、ルートは /ext/doc/ "
        "| /ext/doc-static/t.pdf | 外部 https://example.org/static/x.pdf "
        "旧 [x](/ext/cage/static/a.pdf) /ext/cage/doc/院内/05_転倒 /ext/document/x"
    )
    assert build.internal_links(text) == [
        ("static", "k/docs/14-2.pdf", ""),
        ("static", "k/figs/a b.jpg", ""),
        ("static", "dpc/x.xlsx", ""),
        ("static", "y.csv", ""),
        ("doc", "/院内/05_転倒.md)", ""),  # doc is captured broadly; doc_link_target decides where to cut
        ("doc", "/院内/)", ""),
        ("doc", "/院内/_index.md)", ""),
        ("doc", "/院内/05_転倒", ""),
        ("static", "t.pdf", ""),
        ("legacy", "/ext/cage/static/a.pdf", ""),
        ("legacy", "/ext/cage/doc/院内/05_転倒", ""),
    ]


def test_internal_links_flags_leftover_source_placeholder() -> None:
    """A leftover old-style `{{PUBLIC_BASE}}…` (a name with no value) is reported as dead (legacy)."""
    text = (
        "[図]({{PUBLIC_BASE}}/static/a.jpg) と {{PUBLIC_BASE}}/figs/a.jpg、"
        "{{USER_NAME}} は nucllei の変数"
    )
    assert build.internal_links(text) == [
        ("legacy", "{{PUBLIC_BASE}}/static/a.jpg", ""),
        ("legacy", "{{PUBLIC_BASE}}/figs/a.jpg", ""),
    ]


def test_doc_link_target_cuts_at_punctuation_against_the_tree() -> None:
    """Node names may contain `、` `）` `(` `)`, so trailing punctuation is peeled off while matching against the tree."""
    kinds = {"/院内": True, "/院内/05_転倒": False, "/群": True,
             "/群/01_心、心膜、肺": True, "/群/A100_料（１日につき）": False, "/群/x(材)": False}
    t = build.doc_link_target
    assert t("/院内/05_転倒.md)", kinds) == ("院内/05_転倒", "leaf", False)
    assert t("/院内/)", kinds) == ("院内", "dir", True)
    assert t("/院内/_index.md)", kinds) == ("院内", "index", True)
    assert t("/院内/05_転倒", kinds) == ("院内/05_転倒", "bare", False)
    assert t("/群/01_心、心膜、肺/、次の文", kinds) == ("群/01_心、心膜、肺", "dir", True)
    assert t("/群/A100_料（１日につき）.md）。", kinds) == ("群/A100_料（１日につき）", "leaf", False)
    assert t("/群/x(材).md)", kinds) == ("群/x(材)", "leaf", False)
    assert t("/群/無い.md、次", kinds) == ("群/無い", "leaf", None)


def test_dead_links_are_recorded_not_fatal(src: Path, tmp_path: Path, caplog) -> None:
    """Missing static files, doc nodes and old URLs give a warning + manifest `dead_links`.
    Non-canonical doc links go to manifest `noncanonical_links` (values in canonical form)."""
    static = tmp_path / "static"
    _write(static / "a.jpg", "x")  # {{DOC_STATIC_BASE}}/a.jpg in _index.md exists
    _write(
        src / "マニュアル" / "09_リンク.md",
        "---\ntitle: リンク\n---\n\n"
        "[ok](/ext/doc/マニュアル/RECIST1.1.md) [ok](/ext/doc/%E7%A9%BA/%E5%AD%90.md) "
        "[ok](/ext/doc/マニュアル/)\n\n"
        "[ng](/ext/doc/マニュアル/無い節.md) [ng]({{DOC_STATIC_BASE}}/k/old_name.pdf#page=2)\n\n"
        "[ng](/ext/doc-static/../a.jpg) [ng](/ext/cage/static/a.jpg) [ng]({{PUBLIC_BASE}}/static/a.jpg)\n\n"
        "[odd](/ext/doc/マニュアル/RECIST1.1) [odd](/ext/doc/マニュアル) "
        "[odd]({{DOC_BASE}}/空/_index.md) [odd](/マニュアル/05_転倒)\n",
    )
    with caplog.at_level("WARNING", logger="cage.build"):
        out, m = _build(src, tmp_path, static_root=static)
    assert m["dead_links"] == {
        "/マニュアル/05_転倒": ["/ext/doc-static/f.jpg"],
        "/マニュアル/09_リンク": [
            "/ext/doc/マニュアル/無い節",
            "/ext/doc-static/k/old_name.pdf",
            "/ext/doc-static/../a.jpg",
            "/ext/cage/static/a.jpg",
            "{{PUBLIC_BASE}}/static/a.jpg",
        ],
    }
    assert m["noncanonical_links"] == {
        "/マニュアル/09_リンク": [
            "/ext/doc/マニュアル/RECIST1.1.md", "/ext/doc/マニュアル/", "/ext/doc/空/",
            "/ext/doc/マニュアル/05_転倒.md",
        ],
    }
    assert (out / "マニュアル" / "09_リンク.md").is_file()  # still generated
    assert "6 dead links in 2 nodes" in caplog.text
    assert "4 doc links in 1 nodes are not in the canonical form" in caplog.text
    on_disk = json.loads(build.manifest_path(out).read_text(encoding="utf-8"))
    assert on_disk["dead_links"] == m["dead_links"]
    assert on_disk["noncanonical_links"] == m["noncanonical_links"]


def test_dead_links_skip_static_without_root(src: Path, tmp_path: Path) -> None:
    _out, m = _build(src, tmp_path)  # no static_root: static links are not checked
    assert m["dead_links"] == {}


def test_static_replacement_triggers_relint(src: Path, tmp_path: Path) -> None:
    """Even with an unchanged source, a change in static contents (names) rebuilds and re-lints.

    So that when `make load KIND=docs` replaces only static, we do not trust the old
    manifest's dead_links (0 entries) and pass through.
    """
    static = tmp_path / "static"
    _write(static / "a.jpg", "x")
    _write(static / "f.jpg", "x")
    out, m1 = _build(src, tmp_path, static_root=static)
    assert m1["dead_links"] == {}
    _out, m2 = _build(src, tmp_path, static_root=static)
    assert m2["skipped"] is True
    (static / "f.jpg").rename(static / "f2.jpg")  # renamed; the original name is gone
    _out, m3 = _build(src, tmp_path, static_root=static)
    assert m3["skipped"] is False
    assert m3["dead_links"] == {"/マニュアル/05_転倒": ["/ext/doc-static/f.jpg"]}
    (static / "f.jpg").write_text("x")
    _out, m4 = _build(src, tmp_path, static_root=static)
    assert m4["skipped"] is False and m4["dead_links"] == {}


def test_parallel_build_matches_serial(src: Path, tmp_path: Path) -> None:
    a = tmp_path / "a" / "doc"
    b = tmp_path / "b" / "doc"
    ma = build.build(src, a, VALUES, workers=1)
    mb = build.build(src, b, VALUES, workers=2)
    assert ma["duplicate_headings"] == mb["duplicate_headings"]
    fa = sorted(p.relative_to(a) for p in a.rglob("*") if p.is_file())
    fb = sorted(p.relative_to(b) for p in b.rglob("*") if p.is_file())
    assert fa == fb
    for rel in fa:
        assert (a / rel).read_bytes() == (b / rel).read_bytes(), rel


# ---------------------------------------------------------------------------
# HTML rendering (from the generated tree)
# ---------------------------------------------------------------------------


@pytest.fixture
def tree(src: Path, tmp_path: Path):
    out, _m = _build(src, tmp_path)
    return walk(out)


def _page(tree, *segs: str) -> str:
    return render.render_node(tree, list(segs), render.make_md())


def test_html_title_crumbs_and_no_front_matter(tree) -> None:
    page = _page(tree, "マニュアル", "05_転倒")
    assert "<title>転倒・転落</title>" in page
    assert "<h1>転倒・転落</h1>" in page  # the title h1 has no id
    assert "title: 転倒" not in page  # front matter is not rendered in the body
    assert '<a href="/ext/doc/">知識ベース</a>' in page
    assert '/ext/doc/%E3%83%9E%E3%83%8B%E3%83%A5%E3%82%A2%E3%83%AB/">マニュアル</a>' in page
    assert '<span aria-current="page">転倒・転落</span>' in page
    assert "<script" not in page
    assert "&lt;script&gt;" in page  # raw HTML is escaped
    assert "表<br>改行 &lt;BEL&gt;" in page  # only attribute-less <br> passes through
    assert 'href="javascript' not in page
    assert '<img src="/ext/doc-static/f.jpg"' in page


def test_heading_ids_follow_body_and_ignore_title(tree) -> None:
    """The id is the heading text itself (itertext). An h2 equal to the title does not become -2.
    Duplicates get -2, -3 from the second on (one namespace across levels)."""
    page = _page(tree, "マニュアル", "05_転倒")
    assert '<h2 id="転倒・転落">' in page
    assert 'id="転倒・転落-2"' not in page
    assert '<h2 id="対応">' in page
    assert '<h3 id="対応-2">' in page
    assert '<h2 id="対応-3">' in page
    assert '<h2 id="強調 と code">' in page
    assert '<h2 id="50%減少">' in page


def test_heading_id_fallback_skips_taken_ids() -> None:
    md = render.make_md()
    ids, dups = render.heading_ids(md.parse("## A\n\n## A-2\n\n## A\n\n## ![img](x.png)B<br>C\n"))
    assert ids == ["A", "A-2", "A-3", "BC"]
    assert dups == ["A"]


def _aliases(body: str) -> tuple[list[str], list[list[str]], str]:
    md = render.make_md()
    tokens = md.parse(body)
    ids, _d = render.heading_ids(tokens)
    aliases = render.heading_aliases(tokens, ids)
    return ids, aliases, md.renderer.render(tokens, md.options, {})


def test_heading_aliases_strip_section_numbers() -> None:
    """A numbered heading gets an alias anchor without the number right before it (the heading id is unchanged)."""
    ids, aliases, out = _aliases(
        "## ⅩⅠ．予防措置と効果\n\n### 3. TV、CT、MRI\n\n## ４．まとめ\n\n## IV. 治療\n\n"
        "## (1) 目的\n\n## （２）方法\n\n## ①手順\n\n## 2-3. 細目\n\n"
        "## Ⅱ型糖尿病\n\n## 2024年改訂\n"
    )
    assert ids[0] == "ⅩⅠ．予防措置と効果"
    assert aliases[0] == ["予防措置と効果", "XI.予防措置と効果"]  # number stripped + NFKC
    assert aliases[1] == ["TV、CT、MRI"]
    assert aliases[2][0] == "まとめ" and "4.まとめ" in aliases[2]
    assert aliases[3] == ["治療"]
    assert aliases[4] == ["目的"]
    assert aliases[5][0] == "方法"
    assert aliases[6][0] == "手順"
    assert aliases[7] == ["細目"]
    assert "型糖尿病" not in aliases[8]  # "Ⅱ型" without a separator is not a number
    assert aliases[9] == []
    assert (
        '<a class="al" id="予防措置と効果"></a><a class="al" id="XI.予防措置と効果"></a>'
        '<h2 id="ⅩⅠ．予防措置と効果">' in out
    )


def test_heading_aliases_never_shadow_or_collide() -> None:
    """An alias equal to a real id is dropped; if two headings share an alias both are dropped (no numbering).
    Real ids and -2 numbering do not depend on aliases."""
    ids, aliases, out = _aliases("## 1. 対応\n\n## 2. 対応\n\n## 対応\n\n## 3. 概要\n\n## 3. 概要\n")
    assert ids == ["1. 対応", "2. 対応", "対応", "3. 概要", "3. 概要-2"]
    assert aliases == [[], [], [], [], []]
    assert 'class="al"' not in out
    ids, aliases, _ = _aliases("## 1. 目的\n\n## 目的\n")
    assert ids == ["1. 目的", "目的"] and aliases == [[], []]


def test_heading_aliases_raw_markdown_form() -> None:
    """Headings with `**`, `<sup>` or HTML also get the raw markdown form (what the prompt makes the model copy) as an alias."""
    ids, aliases, out = _aliases(
        "### 曝露源患者が特定不能<sup>注6)</sup>の場合\n\n## **重要** な注意\n\n"
        "## 3. **抗菌薬**の選択\n\n## 普通の見出し\n"
    )
    assert ids[0] == "曝露源患者が特定不能注6)の場合"
    assert aliases[0] == ["曝露源患者が特定不能<sup>注6)</sup>の場合"]
    assert ids[1] == "重要 な注意" and aliases[1] == ["**重要** な注意"]
    # Numbered ones get, besides the rendered-form alias, the raw form and its number-stripped form
    assert ids[2] == "3. 抗菌薬の選択"
    assert aliases[2] == ["抗菌薬の選択", "3. **抗菌薬**の選択", "**抗菌薬**の選択"]
    assert aliases[3] == []  # raw form == rendered text adds no alias
    assert (
        '<a class="al" id="曝露源患者が特定不能&lt;sup&gt;注6)&lt;/sup&gt;の場合"></a>'
        '<h3 id="曝露源患者が特定不能注6)の場合">' in out
    )


def test_heading_aliases_raw_form_follows_collision_rules() -> None:
    """Raw-form aliases follow the same rules: dropped if equal to a real id, both dropped if two headings share one."""
    # Two headings with the same raw form (ids get -2) → the raw alias is shared, so both are dropped
    ids, aliases, _ = _aliases("## A<sup>1</sup>\n\n## A<sup>1</sup>\n")
    assert ids == ["A1", "A1-2"] and aliases == [[], []]
    # Dropped when the raw form equals another heading's real id (an escaped heading rendered literally)
    ids, aliases, _ = _aliases("## **X**\n\n## \\*\\*X\\*\\*\n")
    assert ids[0] == "X" and ids[1] == "**X**"
    assert aliases[0] == []


def test_heading_alias_in_page() -> None:
    page = render.render_page(
        title="t", body="## ⅩⅠ．予防措置と効果\n", crumbs=[], children=[], md=render.make_md()
    )
    assert '<a class="al" id="予防措置と効果"></a>' in page
    assert "a.al:target+" in page  # landing on an alias still highlights the heading


def test_body_h1_equal_to_title_is_not_doubled(tree) -> None:
    page = _page(tree, "マニュアル", "RECIST1.1")
    assert page.count("<h1") == 1
    assert '<h1 id="RECIST">' in page


def test_dir_index_lists_children_dirs_first(tree) -> None:
    root = _page(tree)
    assert '<nav class="crumbs">' not in root
    assert root.index(">マニュアル</a>") < root.index(">空</a>")
    sec = _page(tree, "マニュアル")
    assert "<p>概要。図は /ext/doc-static/a.jpg</p>" in sec
    # Child links are absolute canonical addresses (leaf: `.md`, directory: trailing `/`)
    assert 'href="/ext/doc/%E3%83%9E%E3%83%8B%E3%83%A5%E3%82%A2%E3%83%AB/05_%E8%BB%A2%E5%80%92.md"' in sec
    assert 'href="/ext/doc/%E7%A9%BA/"' in root
    assert ">転倒・転落</a>" in sec and ">RECIST</a>" in sec


def test_old_form_doc_refs_are_left_and_linted(src: Path, tmp_path: Path) -> None:
    """Old prefix-less forms (`/<category>/…`) in the body are not rewritten (no guessing). Only those
    hitting a tree node appear in `noncanonical_links` in canonical form. Nonexistent ones, mid-URL
    occurrences, canonical forms with `{{DOC_BASE}}` and format descriptions are not picked up."""
    _write(
        src / "マニュアル" / "10_関連.md",
        "---\ntitle: 関連\n---\n\n"
        "関連: `/マニュアル/05_転倒` と `/マニュアル` (/マニュアル/RECIST1.1) "
        '`grep_doc(path="/空")` /マニュアル/05_転倒#対応。\n\n'
        "拾わない: /マニュアル/無い節 /マニュアル/05_転倒_x https://h/マニュアル/05_転倒 "
        "`{{DOC_BASE}}/マニュアル/05_転倒.md` `{{DOC_BASE}}/マニュアル/` `{{DOC_BASE}}/マニュアル/…`\n",
    )
    out, m = _build(src, tmp_path)
    body = walk(out).children["マニュアル"].children["10_関連"].body
    assert "関連: `/マニュアル/05_転倒` と `/マニュアル` (/マニュアル/RECIST1.1)" in body
    assert "`/ext/doc/マニュアル/05_転倒.md` `/ext/doc/マニュアル/`" in body
    assert m["noncanonical_links"] == {
        "/マニュアル/10_関連": [
            "/ext/doc/マニュアル/05_転倒.md", "/ext/doc/マニュアル/",
            "/ext/doc/マニュアル/RECIST1.1.md", "/ext/doc/空/",
        ],
    }
    assert m["dead_links"] == {}
