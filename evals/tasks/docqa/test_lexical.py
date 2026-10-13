"""Tests for the fixed, dependency-light lexical docqa baseline."""

from __future__ import annotations

from pathlib import Path

from evals.tasks.docqa.lexical import LexicalIndex


def _write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_ranks_matching_content_and_uses_deterministic_path_tiebreak(tmp_path: Path) -> None:
    _write(tmp_path, "zeta.md", "抗菌薬の投与量を確認する。")
    _write(tmp_path, "alpha.md", "抗菌薬の投与量を確認する。")
    _write(tmp_path, "other.md", "手術室の運用手順。")
    index = LexicalIndex(tmp_path)

    results = index.search("抗菌薬 投与量", k=3)

    assert [result["path"] for result in results[:2]] == ["/alpha", "/zeta"]
    assert results[0]["score"] > 0


def test_canonical_paths_and_metadata_are_available_for_citation(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "manual/restraint/_index.md",
        "---\ndate: 2025-04-01\nsource: hospital manual\n---\n本文です。",
    )
    _write(tmp_path, "guidelines/edition.yaml", "date: 2024-01-01\nsource: society\ntext: 推奨本文\n")
    index = LexicalIndex(tmp_path)

    markdown = index.search("本文", k=1)[0]
    yaml_result = index.search("推奨本文", k=1)[0]

    assert markdown["path"] == "/manual/restraint"
    assert markdown["chunk_id"] == "/manual/restraint#0000"
    assert "path: /manual/restraint" in markdown["text"]
    assert "date: 2025-04-01" in markdown["text"]
    assert "source: hospital manual" in markdown["text"]
    assert yaml_result["path"] == "/guidelines/edition"
    assert "date: 2024-01-01" in yaml_result["text"]
    assert "source: society" in yaml_result["text"]


def test_empty_query_returns_no_arbitrary_context(tmp_path: Path) -> None:
    _write(tmp_path, "guide.md", "診療手順")
    index = LexicalIndex(tmp_path)

    assert index.search("", k=10) == []
    assert index.search("！？。", k=10) == []
    assert index.search("診療", k=0) == []


def test_chunk_boundaries_and_overlap_are_fixed(tmp_path: Path) -> None:
    _write(tmp_path, "long.md", "0123456789ABCDEFGHIJ")
    index = LexicalIndex(tmp_path, chunk_chars=10, overlap_chars=2)

    chunks = sorted(
        (chunk for chunk in index.search("0123456789 abcdefgh ghij", k=10)
         if chunk["path"] == "/long"),
        key=lambda chunk: chunk["chunk_id"],
    )

    assert [chunk["chunk_id"] for chunk in chunks] == ["/long#0000", "/long#0001", "/long#0002"]
    assert [chunk["text"].split("\n\n", 1)[1] for chunk in chunks] == [
        "0123456789", "89ABCDEFGH", "GHIJ",
    ]


def test_manifest_hashes_all_markdown_and_yaml_without_source_omission(tmp_path: Path) -> None:
    _write(tmp_path, "one.md", "uniqueone")
    _write(tmp_path, "nested/two.yaml", "text: uniquetwo\n")
    _write(tmp_path, "ignored.txt", "uniquethree")
    index = LexicalIndex(tmp_path)

    manifest = index.manifest()

    assert manifest["source_file_count"] == 2
    assert manifest["chunk_count"] == 2
    assert len(manifest["corpus_sha256"]) == 64
    assert index.search("uniqueone", k=1)[0]["path"] == "/one"
    assert index.search("uniquetwo", k=1)[0]["path"] == "/nested/two"
    assert index.search("uniquethree", k=1) == []
