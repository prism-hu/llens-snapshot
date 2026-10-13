"""API endpoint tests using FastAPI's TestClient (in-process, no real network)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cage import server
from cage.server import app, loose_note
from cage.walker import DOC_PREFIX, GREP_DEFAULT_LIMIT, STATIC_PREFIX


@pytest.fixture(scope="module")
def client():
    """TestClient as context manager so the lifespan startup runs (builds the tree)."""
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# /doc/list
# ---------------------------------------------------------------------------


def test_lookup_root(client: TestClient) -> None:
    r = client.post("/doc/list", json={})
    assert r.status_code == 200
    d = r.json()
    assert d["path"] == "/ext/doc/"
    assert d["title"] == "知識ベース"
    section_paths = [c["path"] for c in d["children"]]
    assert "/ext/doc/診療ガイドライン/" in section_paths


def test_lookup_response_has_no_instruction_field(client: TestClient) -> None:
    """instruction was removed: it had a strong side effect of making the model answer in plain text."""
    r = client.post("/doc/list", json={})
    d = r.json()
    assert "instruction" not in d


def test_lookup_top_level_node_has_meta(client: TestClient) -> None:
    """The looked-up node's own meta is returned (used to reference guideline name / publisher)."""
    r = client.post("/doc/list", json={"path": "/診療ガイドライン/肺癌2025_非小細胞癌"})
    d = r.json()
    assert "meta" in d
    assert "肺癌診療ガイドライン" in d["meta"]["guideline"]


def test_lookup_hidden_fields_excluded_from_meta(client: TestClient) -> None:
    """`?`-marked provenance fields (source / retrieved_at) do not appear in list meta.

    Unmarked fields (guideline / publisher) do. End-to-end check of list on/off via `?` in
    `fields` (the parent category's `fields` declares source?/retrieved_at? as hidden).
    """
    r = client.post("/doc/list", json={"path": "/診療ガイドライン/肺癌2025_非小細胞癌"})
    meta = r.json()["meta"]
    assert "guideline" in meta and "publisher" in meta  # unmarked → shown
    assert "source" not in meta  # ? → hidden
    assert "retrieved_at" not in meta


def test_lookup_ctcae_leaf_shows_entry_count(client: TestClient) -> None:
    """CTCAE SOC leaves expose entry_count (unmarked, int) in list meta (the count is visible from list)."""
    r = client.post(
        "/doc/list", json={"path": "/診療ガイドライン/CTCAEv5_JCOG_2025/心臓障害"}
    )
    assert r.json()["meta"].get("entry_count") == 34


def test_ctcae_meddra_codes_hidden_from_list(client: TestClient) -> None:
    """meddra_codes is provenance: hidden from list meta via `?` in fields
    (entry_count is unmarked, so shown). Required by the schema, so every SOC has it."""
    r = client.post(
        "/doc/list", json={"path": "/診療ガイドライン/CTCAEv5_JCOG_2025/心臓障害"}
    )
    meta = r.json()["meta"]
    assert "entry_count" in meta
    assert "meddra_codes" not in meta


def test_ctcae_body_has_no_raw_html_comments(client: TestClient) -> None:
    """MedDRA codes moved to the frontmatter. No raw HTML comments (`<!-- ... -->`) remain in
    the read_doc body = removes the trigger for OWUI markdown breakage (docs/cage/07)."""
    r = client.post(
        "/doc/read",
        json={"path": "/診療ガイドライン/CTCAEv5_JCOG_2025/血液およびリンパ系障害"},
    )
    assert "<!--" not in r.text
    assert "MedDRA v20.1 Code" not in r.text


def test_lookup_children_have_no_meta(client: TestClient) -> None:
    """children carry no meta (smaller payload so the model is not distracted)."""
    r = client.post("/doc/list", json={"path": "/"})
    d = r.json()
    for c in d["children"]:
        assert "meta" not in c, f"child {c['path']} unexpectedly has meta"


def test_lookup_children_are_one_layer_only(client: TestClient) -> None:
    """Each children entry has only path/title/body_chars/child_count. No nesting."""
    r = client.post("/doc/list", json={})
    d = r.json()
    for c in d["children"]:
        assert set(c.keys()) == {"path", "title", "body_chars", "child_count"}


def test_lookup_body_chars_always_present(client: TestClient) -> None:
    """`body_chars` is always present on the node and on every children entry (0 = no body)."""
    r = client.post("/doc/list", json={})
    d = r.json()
    assert d["body_chars"] == 0  # root has no body
    r = client.post("/doc/list", json={"path": "/診療ガイドライン/肺癌2025_非小細胞癌"})
    d = r.json()
    assert d["body_chars"] > 0
    for c in d["children"]:
        assert isinstance(c["body_chars"], int)
        assert c["body_chars"] > 0  # every CQ leaf has a body


def test_lookup_section(client: TestClient) -> None:
    r = client.post("/doc/list", json={"path": "/診療ガイドライン/肺癌2025_非小細胞癌"})
    assert r.status_code == 200
    d = r.json()
    assert d["path"] == "/ext/doc/診療ガイドライン/肺癌2025_非小細胞癌/"
    assert d["title"] == "非小細胞肺癌"
    assert "肺癌診療ガイドライン" in d["meta"]["guideline"]
    assert d["meta"]["publisher"] == "日本肺癌学会"


def test_lookup_section_lists_cqs(client: TestClient) -> None:
    r = client.post("/doc/list", json={"path": "/診療ガイドライン/肺癌2025_非小細胞癌"})
    d = r.json()
    cq_paths = sorted(c["path"] for c in d["children"])
    assert "/ext/doc/診療ガイドライン/肺癌2025_非小細胞癌/CQ47.md" in cq_paths
    assert all(p.startswith("/ext/doc/診療ガイドライン/肺癌2025_非小細胞癌/CQ") for p in cq_paths)
    assert len(cq_paths) >= 8


def test_lookup_leaf_has_no_children(client: TestClient) -> None:
    r = client.post("/doc/list", json={"path": "/診療ガイドライン/肺癌2025_非小細胞癌/CQ47"})
    assert r.status_code == 200
    d = r.json()
    assert d["path"] == "/ext/doc/診療ガイドライン/肺癌2025_非小細胞癌/CQ47.md"
    assert d["children"] == []


def test_lookup_never_includes_body(client: TestClient) -> None:
    """/doc/list must never expose body content (use /doc/read for that). Only the length via body_chars."""
    r = client.post("/doc/list", json={"path": "/診療ガイドライン/肺癌2025_非小細胞癌/CQ47"})
    d = r.json()
    assert "body" not in d
    assert "body_markdown" not in d
    assert d["body_chars"] > 0


def test_lookup_body_chars_at_section_and_leaf(client: TestClient) -> None:
    """`body_chars` tells whether calling read here is worthwhile (0 = no body).

    - root (`_index.yaml`) → 0
    - intermediate category (`診療ガイドライン/_index.md`) → > 0 (has the license text)
    - section (`_index.md`) → > 0
    - CQ leaf → > 0
    """
    r = client.post("/doc/list", json={"path": "/"})
    assert r.json()["body_chars"] == 0
    r = client.post("/doc/list", json={"path": "/診療ガイドライン"})
    assert r.json()["body_chars"] > 0
    r = client.post("/doc/list", json={"path": "/診療ガイドライン/肺癌2025_非小細胞癌"})
    assert r.json()["body_chars"] > 0
    r = client.post("/doc/list", json={"path": "/診療ガイドライン/肺癌2025_非小細胞癌/CQ47"})
    assert r.json()["body_chars"] > 0


def test_lookup_child_count_at_leaf(client: TestClient) -> None:
    """A leaf has child_count=0 (the signal telling the AI "call read here")."""
    r = client.post("/doc/list", json={"path": "/診療ガイドライン/肺癌2025_非小細胞癌/CQ47"})
    d = r.json()
    assert d["child_count"] == 0


def test_lookup_child_count_signals_drill_down(client: TestClient) -> None:
    """When children have grandchildren, child_count > 0 signals "there is more below"."""
    r = client.post("/doc/list", json={"path": "/診療ガイドライン"})
    d = r.json()
    # every section contains CQs
    for section in d["children"]:
        assert section["child_count"] >= 1


def test_lookup_child_count_matches_expanded_children(client: TestClient) -> None:
    r = client.post("/doc/list", json={"path": "/診療ガイドライン/肺癌2025_非小細胞癌"})
    d = r.json()
    assert d["child_count"] == len(d["children"])
    assert d["child_count"] >= 8


def test_lookup_not_found_returns_siblings(client: TestClient) -> None:
    r = client.post(
        "/doc/list", json={"path": "/診療ガイドライン/肺癌2025_非小細胞癌/typoCQ"}
    )
    assert r.status_code == 404
    d = r.json()
    assert d["error"] == "not_found"
    assert d["missing"] == "typoCQ"
    assert d["valid_path"] == "/ext/doc/診療ガイドライン/肺癌2025_非小細胞癌/"
    assert "CQ47.md" in d["siblings_at_parent"]


def test_lookup_not_found_at_root(client: TestClient) -> None:
    r = client.post("/doc/list", json={"path": "/存在しないセクション"})
    assert r.status_code == 404
    d = r.json()
    assert d["missing"] == "存在しないセクション"
    assert d["valid_path"] == "/ext/doc/"
    assert "診療ガイドライン/" in d["siblings_at_parent"]


def test_lookup_bad_path_no_leading_slash(client: TestClient) -> None:
    """A path not starting with `/` is 400."""
    r = client.post("/doc/list", json={"path": "診療ガイドライン"})
    assert r.status_code == 400
    d = r.json()
    assert d["error"] == "bad_path"


# ---------------------------------------------------------------------------
# /doc/read
# ---------------------------------------------------------------------------


def test_read_leaf_returns_markdown(client: TestClient) -> None:
    r = client.post("/doc/read", json={"path": "/診療ガイドライン/肺癌2025_非小細胞癌/CQ47"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert "FLAURA" in r.text
    assert "## 推奨" in r.text
    assert "## 解説" in r.text
    assert "## 参考文献" in r.text


def test_read_section_with_body(client: TestClient) -> None:
    """The body of a section (`_index.md`) can be read."""
    r = client.post("/doc/read", json={"path": "/診療ガイドライン/肺癌2025_小細胞癌"})
    assert r.status_code == 200
    assert "小細胞肺癌" in r.text or "SCLC" in r.text


def test_read_root_returns_empty(client: TestClient) -> None:
    """data/doc/_index.yaml has no body → empty string."""
    r = client.post("/doc/read", json={"path": "/"})
    assert r.status_code == 200
    assert r.text == ""


def test_read_not_found(client: TestClient) -> None:
    r = client.post("/doc/read", json={"path": "/nonexistent"})
    assert r.status_code == 404
    d = r.json()
    assert d["error"] == "not_found"
    assert "診療ガイドライン/" in d["siblings_at_parent"]


# ---------------------------------------------------------------------------
# deploy value substitution (done at startup by the build. build.py)
# ---------------------------------------------------------------------------
#
# Figure / original PDF URLs are written as `{{DOC_STATIC_BASE}}/...` in the source and are
# replaced at build time with the fixed `/ext/doc-static/...` (same-server relative URL = the
# sandbox path) (build.FIXED_PLACEHOLDERS; `{{DOC_BASE}}` in doc paths likewise). The request's
# public_origin (sent by old tools) and the LLENS entry point are never prepended. The deploy
# value of {{HIS_FAQ_BASE}} is pinned by conftest.py in tests.


def _node_with(client: TestClient, literal: str) -> str:
    """Return the path of one node whose built body contains literal (skip if none)."""
    r = client.post("/doc/grep", json={"query": literal, "path": "/", "limit": 1})
    d = r.json()
    if d["match_count"] == 0:
        pytest.skip(f"no doc containing {literal} is included")
    return d["matches"][0]["path"]


def test_no_deploy_placeholder_survives(client: TestClient) -> None:
    """No deploy value placeholder remains in the build output seen by read / grep."""
    for ph in (
        r"\{\{PUBLIC_BASE\}\}", r"\{\{DOC_BASE\}\}", r"\{\{DOC_STATIC_BASE\}\}",
        r"\{\{HIS_FAQ_BASE\}\}",
    ):
        # regex is used so the loose fallback (word segmentation) is not triggered
        r = client.post("/doc/grep", json={"query": ph, "path": "/", "regex": True})
        assert r.status_code == 200
        assert r.json()["match_count"] == 0, ph


def test_read_static_urls_are_fixed_relative(client: TestClient) -> None:
    """Figures / originals are `/ext/doc-static/…` (kept relative, no origin prepended). No old URLs remain."""
    path = _node_with(client, "(/ext/doc-static/")
    r = client.post("/doc/read", json={"path": path})
    assert r.status_code == 200
    assert "{{DOC_STATIC_BASE}}" not in r.text
    assert "(/ext/doc-static/" in r.text
    old = client.post("/doc/grep", json={"query": "/ext/cage/", "path": "/", "regex": True})
    assert old.json()["match_count"] == 0


def test_read_ignores_public_origin_from_old_tools(client: TestClient) -> None:
    """public_origin sent by old tools is neither rejected with 400 nor used (unknown fields are ignored)."""
    path = _node_with(client, "(/ext/doc-static/")
    for origin in ("https://192.0.2.1", "https://192.0.2.1/../x", ""):
        r = client.post("/doc/read", json={"path": path, "public_origin": origin})
        assert r.status_code == 200, origin
        assert "(/ext/doc-static/" in r.text
        assert "192.0.2.1" not in r.text
    r = client.post(
        "/doc/grep", json={"query": "x", "path": "/", "public_origin": "https://h/../p"}
    )
    assert r.status_code == 200


def test_grep_snippet_uses_same_static_urls(client: TestClient) -> None:
    """grep snippets are cut from the build output too, so they carry the same values as read."""
    path = _node_with(client, "(/ext/doc-static/")
    r = client.post("/doc/grep", json={"query": "/ext/doc-static/", "path": path})
    assert r.status_code == 200
    snippet = r.json()["matches"][0]["body_first_match_snippet"]
    assert "/ext/doc-static/" in snippet


def test_grep_snippet_substitutes_his_faq_base(client: TestClient) -> None:
    """The FAQ original link {{HIS_FAQ_BASE}} is also substituted at build time."""
    path = _node_with(client, "https://his.example/his-info/")
    r = client.post("/doc/read", json={"path": path})
    assert "{{HIS_FAQ_BASE}}" not in r.text
    assert "https://his.example/his-info/" in r.text


def test_nucllei_variables_are_left_alone(client: TestClient) -> None:
    """nucllei's own variables ({{CURRENT_DATE}} etc.) are filled by nucllei, so the build leaves them alone."""
    r = client.post("/doc/grep", json={"query": "{{CURRENT_DATE}}", "path": "/"})
    if r.json()["match_count"] == 0:
        pytest.skip("no doc containing {{CURRENT_DATE}} is included")
    path = r.json()["matches"][0]["path"]
    assert "{{CURRENT_DATE}}" in client.post("/doc/read", json={"path": path}).text


# ---------------------------------------------------------------------------
# /doc/grep
# ---------------------------------------------------------------------------


def test_grep_substring_body_hit(client: TestClient) -> None:
    """A body match is returned as match_count.body>=1 + body_first_match_snippet (±40 chars)."""
    r = client.post(
        "/doc/grep",
        json={"query": "オシメルチニブ", "path": "/診療ガイドライン/肺癌2025_非小細胞癌"},
    )
    assert r.status_code == 200
    d = r.json()
    assert d["match_count"] >= 1
    assert d["has_more"] is False
    paths = [m["path"] for m in d["matches"]]
    assert "/ext/doc/診療ガイドライン/肺癌2025_非小細胞癌/CQ47.md" in paths
    body_hits = [m for m in d["matches"] if m["match_count"]["body"] >= 1]
    assert body_hits, "expected at least one body hit"
    assert "オシメルチニブ" in body_hits[0]["body_first_match_snippet"]


def test_grep_title_and_body_both_reported(client: TestClient) -> None:
    """When both title and body hit on the same node, match_count shows >= 1 for both."""
    r = client.post(
        "/doc/grep",
        json={"query": "EGFR", "path": "/診療ガイドライン/肺癌2025_非小細胞癌"},
    )
    assert r.status_code == 200
    d = r.json()
    # at least one node with title=1
    title_hits = [m for m in d["matches"] if m["match_count"]["title"] >= 1]
    assert title_hits, "expected at least one title hit"
    # of those, at least one node also hits in body
    both = [m for m in title_hits if m["match_count"]["body"] >= 1]
    assert both, "expected at least one node where title and body both hit"


def test_grep_case_insensitive(client: TestClient) -> None:
    r_upper = client.post("/doc/grep", json={"query": "EGFR", "path": "/"}).json()
    r_lower = client.post("/doc/grep", json={"query": "egfr", "path": "/"}).json()
    assert r_upper["match_count"] == r_lower["match_count"]


def test_grep_nfkc_normalizes_fullwidth(client: TestClient) -> None:
    """NFKC: fullwidth ＡＮＣ and halfwidth ANC return the same hits (ANC is scattered through CTCAE)."""
    r_half = client.post(
        "/doc/grep", json={"query": "ANC", "path": "/診療ガイドライン/CTCAEv5_JCOG_2025"}
    ).json()
    r_full = client.post(
        "/doc/grep", json={"query": "ＡＮＣ", "path": "/診療ガイドライン/CTCAEv5_JCOG_2025"}
    ).json()
    assert r_half["match_count"] == r_full["match_count"]
    assert r_half["match_count"] >= 1


def test_grep_root_aliases(client: TestClient) -> None:
    """'/', '', '*' and '/*' all mean the root subtree."""
    base = client.post("/doc/grep", json={"query": "肺癌", "path": "/"}).json()
    assert base["path"] == "/ext/doc/"
    for alias in ("", "*", "/*"):
        r = client.post("/doc/grep", json={"query": "肺癌", "path": alias}).json()
        assert r["path"] == "/ext/doc/"
        assert r["match_count"] == base["match_count"]


def test_grep_regex_mode(client: TestClient) -> None:
    """regex=True switches to regular-expression matching."""
    r = client.post(
        "/doc/grep",
        json={
            "query": r"Grade\s*[34]",
            "path": "/診療ガイドライン/CTCAEv5_JCOG_2025/血液およびリンパ系障害",
            "regex": True,
        },
    )
    assert r.status_code == 200
    d = r.json()
    assert d["regex"] is True
    assert d["match_count"] >= 1


def test_grep_regex_compile_error(client: TestClient) -> None:
    r = client.post("/doc/grep", json={"query": "(unbalanced", "regex": True})
    assert r.status_code == 400
    d = r.json()
    assert d["error"] == "bad_regex"
    assert "message" in d


def test_grep_empty_query(client: TestClient) -> None:
    r = client.post("/doc/grep", json={"query": ""})
    assert r.status_code == 400
    d = r.json()
    assert d["error"] == "bad_query"


def test_grep_path_not_found(client: TestClient) -> None:
    r = client.post("/doc/grep", json={"query": "test", "path": "/typo"})
    assert r.status_code == 404
    d = r.json()
    assert d["error"] == "not_found"
    assert d["missing"] == "typo"
    assert "診療ガイドライン/" in d["siblings_at_parent"]


def test_grep_paging(client: TestClient) -> None:
    """Many hits are cut at limit and the rest is reachable via has_more / next_offset.

    No branching — with `if truncated`, a broken GREP_DEFAULT_LIMIT returning everything would
    still pass via the else side. "症状" reliably exceeds the default of 10.
    """
    r = client.post("/doc/grep", json={"query": "症状", "path": "/"}).json()
    assert r["offset"] == 0
    assert r["returned"] == len(r["matches"]) == GREP_DEFAULT_LIMIT
    assert r["match_count"] > r["returned"], "the query is assumed to exceed the default count"
    assert r["has_more"] is True
    assert r["next_offset"] == r["returned"]


def test_grep_last_page_has_no_continuation(client: TestClient) -> None:
    """On the last page has_more=False and no next_offset."""
    total = client.post(
        "/doc/grep", json={"query": "オシメルチニブ", "path": "/診療ガイドライン"}
    ).json()["match_count"]
    r = client.post(
        "/doc/grep",
        json={
            "query": "オシメルチニブ",
            "path": "/診療ガイドライン",
            "limit": 5,
            "offset": max(0, total - 1),
        },
    ).json()
    assert r["has_more"] is False
    assert "next_offset" not in r


def test_grep_sorted_by_relevance(client: TestClient) -> None:
    """Sorted by relevance descending (not the old tree order)."""
    r = client.post(
        "/doc/grep", json={"query": "症状", "path": "/", "limit": 20}
    ).json()
    def score(m: dict) -> int:
        mc = m["match_count"]
        return mc["title"] * 10 + mc["description"] * 5 + mc["body"]

    scores = [score(m) for m in r["matches"]]
    assert scores == sorted(scores, reverse=True)


def test_grep_offset_continues_without_overlap(client: TestClient) -> None:
    """The continuation fetched via offset does not overlap the first page.

    Too few hits fails rather than skips — a skip would turn a broken search that no longer
    hits into a "silently unverified" state.
    """
    a = client.post(
        "/doc/grep", json={"query": "症状", "path": "/", "limit": 5}
    ).json()
    assert a["has_more"] is True, "the query is assumed to produce 2 pages of hits"
    b = client.post(
        "/doc/grep",
        json={"query": "症状", "path": "/", "limit": 5, "offset": a["next_offset"]},
    ).json()
    assert b["offset"] == a["next_offset"]
    assert not ({m["path"] for m in a["matches"]} & {m["path"] for m in b["matches"]})


def test_grep_limit_bounds(client: TestClient) -> None:
    """limit is 1..GREP_MAX_LIMIT; out of range is rejected with 422."""
    assert client.post("/doc/grep", json={"query": "症状", "limit": 0}).status_code == 422
    assert (
        client.post("/doc/grep", json={"query": "症状", "limit": 999}).status_code == 422
    )
    assert (
        client.post("/doc/grep", json={"query": "症状", "offset": -1}).status_code == 422
    )


def test_grep_snippet_no_newlines(client: TestClient) -> None:
    """Newlines in body_first_match_snippet are replaced by spaces (flattened to one line)."""
    r = client.post(
        "/doc/grep",
        json={"query": "オシメルチニブ", "path": "/診療ガイドライン/肺癌2025_非小細胞癌"},
    ).json()
    for m in r["matches"]:
        snip = m.get("body_first_match_snippet")
        if snip is not None:
            assert "\n" not in snip


def test_grep_match_entry_shape(client: TestClient) -> None:
    """Every entry always has path/title/body_chars/child_count/match_count;
    description only on a description hit, body_first_match_snippet only on a body hit.
    """
    r = client.post("/doc/grep", json={"query": "EGFR", "path": "/診療ガイドライン"}).json()
    required = {"path", "title", "body_chars", "child_count", "match_count"}
    allowed = required | {"description", "body_first_match_snippet"}
    for m in r["matches"]:
        keys = set(m.keys())
        assert required.issubset(keys), f"missing required keys: {required - keys}"
        assert keys.issubset(allowed), f"unexpected keys: {keys - allowed}"
        assert isinstance(m["body_chars"], int) and m["body_chars"] >= 0
        mc = m["match_count"]
        assert set(mc.keys()) == {"title", "description", "body"}
        assert mc["title"] in (0, 1)
        assert mc["description"] in (0, 1)
        assert isinstance(mc["body"], int) and mc["body"] >= 0
        # at least one field hit
        assert mc["title"] + mc["description"] + mc["body"] >= 1
        # consistency of the conditional fields
        if mc["description"] >= 1:
            assert "description" in m
        else:
            assert "description" not in m
        if mc["body"] >= 1:
            assert "body_first_match_snippet" in m
        else:
            assert "body_first_match_snippet" not in m


def test_grep_body_match_count_is_total(client: TestClient) -> None:
    """match_count.body is the total number of hits in the body, not a boolean."""
    r = client.post(
        "/doc/grep",
        json={"query": "EGFR", "path": "/診療ガイドライン/肺癌2025_非小細胞癌"},
    ).json()
    body_counts = [m["match_count"]["body"] for m in r["matches"]]
    assert any(c >= 2 for c in body_counts), (
        f"expected at least one node with >=2 body hits, got {body_counts}"
    )


# ---------------------------------------------------------------------------
# /doc/grep — loose fallback (when the literal match has 0 hits)
# ---------------------------------------------------------------------------


EXACT_TOP_KEYS = {
    "path",
    "query",
    "regex",
    "mode",
    "match_count",
    "offset",
    "returned",
    "has_more",
    "matches",
}
LOOSE_EXTRA_TOP_KEYS = {"terms", "absent_terms", "ignored_terms", "note"}
ENTRY_REQUIRED = {"path", "title", "body_chars", "child_count", "match_count"}
ENTRY_OPTIONAL = {"description", "body_first_match_snippet"}


def test_grep_exact_names_its_mode(client: TestClient) -> None:
    """On a literal match it reports mode=exact and none of the loose keys are attached.

    The agreed spec "an exact response only gains the one key mode" is pinned by set equality.
    A partial not-in check would let absent_terms etc. slip in, opening a path for the model to
    misread exact results as loose.
    """
    d = client.post(
        "/doc/grep",
        json={"query": "オシメルチニブ", "path": "/診療ガイドライン"},
    ).json()
    assert d["match_count"] >= 1
    assert d["mode"] == "exact"
    assert set(d) - {"next_offset"} == EXACT_TOP_KEYS, (
        f"exact response keys differ from the spec: extra {sorted(set(d) - EXACT_TOP_KEYS - {'next_offset'})} / "
        f"missing {sorted(EXACT_TOP_KEYS - set(d))}"
    )
    for m in d["matches"]:
        assert set(m) - ENTRY_OPTIONAL == ENTRY_REQUIRED
        assert "matched_terms" not in m


def test_grep_loose_response_shape_is_exactly_the_documented_one(
    client: TestClient,
) -> None:
    """loose adds exactly 4 keys (+ matched_terms per entry). No internal values leak.

    In particular, not exposing the raw BM25 score is a deliberate design decision (showing an
    uncalibrated float makes the model start cutting off results without basis). Pinned by set
    equality.
    """
    d = client.post(
        "/doc/grep",
        json={"query": "オシメルチニブの副作用", "path": "/診療ガイドライン", "limit": 5},
    ).json()
    assert d["mode"] == "loose"
    assert set(d) - {"next_offset"} == EXACT_TOP_KEYS | LOOSE_EXTRA_TOP_KEYS
    assert d["matches"], "this query is assumed to hit at least once in loose mode"
    for m in d["matches"]:
        keys = set(m)
        assert ENTRY_REQUIRED | {"matched_terms"} <= keys
        assert keys <= ENTRY_REQUIRED | ENTRY_OPTIONAL | {"matched_terms"}, (
            f"unexpected keys in loose entry: {sorted(keys - ENTRY_REQUIRED - ENTRY_OPTIONAL - {'matched_terms'})}"
        )
        # the conditions of the conditional keys are pinned too (same rules as exact)
        assert ("description" in m) == (m["match_count"]["description"] >= 1)
        assert ("body_first_match_snippet" in m) == (m["match_count"]["body"] >= 1)


def test_grep_falls_back_to_loose_and_says_so(client: TestClient) -> None:
    """A natural-language query with 0 literal hits falls back to word segmentation (BM25) and says so.

    Silently swapping in a different search makes the model believe the query existed literally.
    """
    d = client.post(
        "/doc/grep",
        json={"query": "オシメルチニブの副作用", "path": "/診療ガイドライン"},
    ).json()
    assert d["mode"] == "loose"
    assert d["terms"] == ["オシメルチニブ", "副作用"]
    assert d["match_count"] >= 1
    assert "語に分けて探した" in d["note"] and "read_doc" in d["note"]
    for m in d["matches"]:
        assert m["matched_terms"], "a loose entry always carries the terms that hit"
        assert set(m["matched_terms"]) <= set(d["terms"])


def test_grep_loose_ranks_all_terms_first(client: TestClient) -> None:
    """A node matching all terms ranks above one with many hits on a single term (coord factor).

    Observed regression: without coord, a document with TDM 4 times beat one containing both
    terms once each and took rank 1.

    **The coord factor alone is not enough (issue #6, 2026-09-04).** The expected hit here is
    long (24,018 chars); while the length-normalization baseline (avgdl) was taken over the
    whole search scope, **merely adding the 2,590 short-bodied 診療報酬 nodes** dropped avgdl
    from 2,961 to 2,252 chars, the long hit was punished harder and lost to a 2,246-char page
    with only tdm (rank 1 → 3). Fixed by taking avgdl per group (see the note on `LOOSE_B`).
    Invariance to corpus size itself is checked on a synthetic tree by
    `test_walker.py::test_loose_rank_survives_a_short_document_corpus_being_added`.
    """
    d = client.post(
        "/doc/grep", json={"query": "バンコマイシンのTDM", "path": "/", "limit": 10}
    ).json()
    assert d["mode"] == "loose"
    assert sorted(d["matches"][0]["matched_terms"]) == ["tdm", "バンコマイシン"]


def test_grep_loose_finds_the_topic_node_not_just_the_rare_word(
    client: TestClient,
) -> None:
    """The rare term is not required; the topic node is found even without that literal.

    Observed regression: requiring "接触予防策" (df=5) dropped the MRSA node of the
    per-pathogen manual (104 occurrences of MRSA) entirely.
    """
    d = client.post(
        "/doc/grep", json={"query": "MRSAの接触予防策", "path": "/", "limit": 5}
    ).json()
    assert d["mode"] == "loose"
    paths = [m["path"] for m in d["matches"]]
    assert any("メチシリン耐性黄色ブドウ球菌" in p for p in paths), paths


def test_grep_loose_ignores_ubiquitous_terms_and_says_so(client: TestClient) -> None:
    """Low-information terms are excluded from the search and reported via ignored_terms and note.

    Observed regression: 事故/起/対応 made up more than half the score, and the expected hit
    (occupational infection control, title containing "針刺し") lost to per-department pages,
    falling to rank 8.

    **This test has broken twice by sitting on a threshold boundary** (issue #6). First while it
    asserted "the top 3 are all 14_職業感染対策": the population in `df > 10% of population` was
    the number of included docs, so adding / removing unrelated sections moved "事故" in and out.
    Second after the decision moved to `LOOSE_MIN_IDF_RATIO` (IDF ratio within the query):
    **the IDF ratio of "起" drifted from 0.49 at 930 nodes to 0.50 at 3,895 nodes, crossing the
    0.5 threshold**, and this chapter sank from rank 2 to 9 (`idf ≒ log(N/df)` approaches
    ratio 1 as N grows = the larger the corpus, the more common terms survive).
    It is now dropped on a size-independent basis, the term's **length**
    (`LOOSE_DROP_LONE_KANJI`).

    So this test asserts **only what holds regardless of corpus size**:

    - the excluded terms are reported. "対応" has an IDF ratio of 0.30–0.35, well away from the
      threshold, and "起" is an okurigana remnant (lone kanji), dropped regardless of df.
      Bracketing the threshold boundary itself is done by the synthetic trees in test_walker.
    - the chapter holding the answer for needlestick accidents (14_職業感染対策) is near the
      top of the default page.

    **"How many of the default 10 are from this chapter" is not counted** — observed, that
    number wavers between 1 and 3 and flips when b moves from 0.25 to 0.05. That is exactly
    boundary-sitting, so it stays out.

    That the rank itself does not move with corpus size is covered in test_walker.py by
    `test_loose_rank_does_not_move_when_unrelated_sections_are_added_or_removed`
    (removing from real data) and
    `test_loose_rank_survives_a_short_document_corpus_being_added`
    (adding to a synthetic tree). Doubling that up here would sit on the boundary again.
    """
    d = client.post(
        "/doc/grep",
        json={"query": "針刺し事故が起きたときの対応", "path": "/"},
    ).json()
    assert d["mode"] == "loose"
    for term in ("対応", "起"):
        assert term in d["ignored_terms"], (term, d["ignored_terms"])
    ignored_in_matches = [
        t for m in d["matches"] for t in m["matched_terms"] if t in ("対応", "起")
    ]
    assert not ignored_in_matches, (
        "excluded terms affect neither the score nor the hit condition = not in matched_terms either"
    )
    assert "ありふれ" in d["note"]

    paths = [m["path"] for m in d["matches"]]
    section = "/ext/doc/院内マニュアル/感染対策マニュアル/14_職業感染対策/"
    # While pinned to "top 3", one more legitimate node with "針刺し事故" in its title was
    # enough to fail it (2026-09-06: chapter 10 of プリオン病感染予防2020,
    # 「剖検・病理標本作成（含む針刺し事故）」, took rank 1 and 14-1 went from 3 to 4).
    # Per the docstring's principle, only "is within the default page (10)" is checked.
    assert any(p.startswith(section) for p in paths[:10]), (
        f"the chapter holding the needlestick answer is not on the default page: {paths[:10]}"
    )


def test_grep_loose_names_terms_that_exist_nowhere(client: TestClient) -> None:
    """Terms absent from the corpus are named in absent_terms, and note mentions possible non-coverage (without forcing a verdict)."""
    d = client.post(
        "/doc/grep",
        json={"query": "架空の薬剤ゾルゲンスマックスの投与量", "path": "/"},
    ).json()
    assert d["mode"] == "loose"
    assert "ゾルゲンスマックス" in d["absent_terms"]
    assert d["note"].startswith("次の語はこの範囲の収録文書に 1 件も出てこない")
    assert "薬剤名・製品名などの固有名詞" in d["note"]


# The note text drives the model's behaviour = it is the spec itself, so the pure function
# (server.loose_note) is pinned directly, independent of the included data. Pinning
# `absent_terms == [...]` on real data would fail as soon as one such term entered the corpus
# (a false positive from a revision, not a spec violation).


def test_loose_note_alarms_hard_for_proper_nouns() -> None:
    """A miss on a term with kana/alnum (drug / product names) = possibly not covered, but no snap
    verdict (aligned with the doc section of llens-thinking, "no snap verdict even in loose";
    alternative spellings / abbreviations are tried first)."""
    note = loose_note(["ゾルゲンスマックス", "投与量"], ["ゾルゲンスマックス"], [])
    assert note.startswith("次の語はこの範囲の収録文書に 1 件も出てこない: ゾルゲンスマックス。")
    assert "薬剤名・製品名などの固有名詞" in note
    assert "収録が無い可能性" in note and "別表記・略語でも探してから判断する" in note
    assert "該当なし" not in note
    assert "短く分けて" not in note


def test_loose_note_softens_the_alarm_for_kanji_compounds() -> None:
    """A miss on a kanji-only compound is not alarming; usually the segmentation was just coarse.

    Observed: 2 of 10 realistic queries produced absent, but both were 「副作用管理」
    「副作用対策」 while 「副作用」 itself is abundant = false alarms.
    """
    note = loose_note(["チェックポイント", "副作用管理"], ["副作用管理"], [])
    assert "固有名詞" not in note
    assert "短く分けて" in note
    assert note.startswith("次の語はこの並びのままでは出てこない: 副作用管理。")


def test_loose_note_reports_both_kinds_separately() -> None:
    """When both occur, the proper-noun warning comes first. Ordered by actionability."""
    note = loose_note(
        ["ゾルゲンスマックス", "副作用管理", "対応"], ["ゾルゲンスマックス", "副作用管理"], ["対応"]
    )
    assert note.index("固有名詞") < note.index("短く分けて")
    assert "ありふれすぎるので外した語: 対応。" in note


def test_loose_note_always_states_the_fallback_and_the_guardrail() -> None:
    """Even with no misses, being loose and the read_doc verification are always stated."""
    note = loose_note(["mrsa", "接触予防策"], [], [])
    assert "字面は 0 件" in note and "語に分けて探した" in note
    assert "効かせた語: mrsa、接触予防策。" in note
    assert "read_doc" in note and "matched_terms" in note and "match_count" in note
    assert "ありふれすぎるので外した語" not in note


def test_loose_note_is_a_fact_then_the_next_step() -> None:
    """nucllei convention (README_API §5.1): the factual sentence (safe to relay to the user) first;
    instructions to the model only after 「次の手: 」."""
    note = loose_note(
        ["ゾルゲンスマックス", "副作用管理", "対応", "投与"], ["ゾルゲンスマックス", "副作用管理"], ["対応"]
    )
    assert note.count("次の手: ") == 1
    fact, nxt = note.split("次の手: ")
    assert fact.startswith("次の語はこの範囲の収録文書に 1 件も出てこない")
    for word in ("探して", "掛け直す", "確かめる", "判断する", "read_doc"):
        assert word not in fact, word
    assert "別表記・略語でも探してから判断する" in nxt and "短く分けて" in nxt and "read_doc" in nxt


def test_loose_note_does_not_leak_internal_terms() -> None:
    """The model may copy it into its answer, so no implementation terms or Python list notation."""
    note = loose_note(
        ["ゾルゲンスマックス", "副作用管理", "対応", "投与"], ["ゾルゲンスマックス", "副作用管理"], ["対応"]
    )
    for word in ("BM25", "フォールバック", "subtree", "['", "疑うこと"):
        assert word not in note, word


def test_loose_note_lists_only_the_terms_that_actually_worked() -> None:
    """Both absent and ignored terms are excluded from 「効かせた語」 (no lying)."""
    note = loose_note(["針刺", "事故", "架空語"], ["架空語"], ["事故"])
    assert "効かせた語: 針刺。" in note


def test_grep_no_fallback_when_query_is_one_term(client: TestClient) -> None:
    """A query that splits into a single term is the same in loose as in exact; return 0 hits without the loose label."""
    d = client.post(
        "/doc/grep", json={"query": "ゾルゲンスマックス", "path": "/"}
    ).json()
    assert d["mode"] == "exact"
    assert d["match_count"] == 0
    assert d["matches"] == []


def test_grep_no_fallback_in_regex_mode(client: TestClient) -> None:
    """regex is a tool for matching exactly as written, so it never falls back to word segmentation."""
    d = client.post(
        "/doc/grep",
        json={"query": r"ゾルゲンスマックス.*投与量", "path": "/", "regex": True},
    ).json()
    assert d["mode"] == "exact"
    assert d["match_count"] == 0


def test_grep_loose_paginates_like_exact(client: TestClient) -> None:
    """offset/limit and has_more/next_offset mean the same in loose as in exact."""
    first = client.post(
        "/doc/grep", json={"query": "転倒 報告書", "path": "/", "limit": 3}
    ).json()
    assert first["mode"] == "loose"
    assert first["returned"] == 3
    assert first["has_more"] is True
    second = client.post(
        "/doc/grep",
        json={
            "query": "転倒 報告書",
            "path": "/",
            "limit": 3,
            "offset": first["next_offset"],
        },
    ).json()
    assert second["mode"] == "loose"
    assert second["match_count"] == first["match_count"]
    assert not (
        {m["path"] for m in second["matches"]} & {m["path"] for m in first["matches"]}
    )


def test_grep_loose_order_is_deterministic_across_pages(client: TestClient) -> None:
    """loose rebuilds df/idf over the whole subtree each time; the order must still not move.

    If ranks waver across pages, a model fetching the continuation via offset reads the same
    node twice or never reaches some node. Pinned by requiring the concatenation to match a
    single call **down to the path sequence** (set equality would miss order changes).
    """
    q = {"query": "転倒 報告書", "path": "/"}
    p1 = client.post("/doc/grep", json={**q, "limit": 3}).json()
    p2 = client.post("/doc/grep", json={**q, "limit": 3, "offset": 3}).json()
    single = client.post("/doc/grep", json={**q, "limit": 6}).json()
    assert [m["path"] for m in p1["matches"] + p2["matches"]] == [
        m["path"] for m in single["matches"]
    ]


def test_grep_loose_repeats_identically(client: TestClient) -> None:
    """The same request twice gives the same order; floating-point scores must not reorder."""
    q = {"query": "中心静脈カテーテル挿入時の合併症", "path": "/", "limit": 10}
    a = client.post("/doc/grep", json=q).json()
    b = client.post("/doc/grep", json=q).json()
    assert a == b


# ---------------------------------------------------------------------------
# public routes GET / HEAD /ext/doc/… and /ext/doc-static/… (relayed by the nucllei proxy)
# ---------------------------------------------------------------------------

D = DOC_PREFIX
SAFETY = "/院内マニュアル/医療安全管理マニュアル"  # directory (canonical form has a trailing `/`)


def _leaf_of(client: TestClient, dir_path: str) -> str:
    """One canonical address (`….md`) of a leaf directly under dir_path."""
    d = client.post("/doc/list", json={"path": dir_path}).json()
    return next(c["path"] for c in d["children"] if c["child_count"] == 0)


def test_public_doc_page_is_html_with_csp(client: TestClient) -> None:
    r = client.get(f"{D}{SAFETY}/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "script-src" not in r.headers["content-security-policy"]
    assert r.headers["content-security-policy"].startswith("default-src 'none'")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "<title>医療安全管理マニュアル</title>" in r.text
    assert "<script" not in r.text
    assert '<ul class="children">' in r.text  # node with children = index
    assert r.headers["etag"]


def test_public_doc_other_forms_redirect_to_canonical(client: TestClient) -> None:
    """Alternative spellings the tool path accepts are 302-redirected to the canonical form (one address per page)."""
    from urllib.parse import quote, unquote

    leaf = _leaf_of(client, SAFETY)
    cases = {
        f"{D}{SAFETY}": f"{D}{SAFETY}/",
        f"{D}{SAFETY}.md": f"{D}{SAFETY}/",
        f"{D}{SAFETY}/_index.md": f"{D}{SAFETY}/",
        leaf[: -len(".md")]: leaf,
        D: f"{D}/",
        f"{D}/_index.md": f"{D}/",
    }
    for src, dst in cases.items():
        r = client.get(src, follow_redirects=False)
        assert r.status_code == 302, src
        assert unquote(r.headers["location"]) == dst, src
        assert r.headers["location"] == quote(dst, safe="/:")  # percent-encoded
    # the query string is re-attached (the browser carries the fragment)
    r = client.get(f"{D}{SAFETY}?x=1", follow_redirects=False)
    assert r.headers["location"].endswith("/?x=1")
    # the canonical form is 200 as is
    for ok in (f"{D}/", f"{D}{SAFETY}/", leaf):
        assert client.get(ok, follow_redirects=False).status_code == 200, ok


def test_public_doc_leaf_page(client: TestClient) -> None:
    leaf = _leaf_of(client, SAFETY)
    r = client.get(leaf)
    assert r.status_code == 200 and "<h1" in r.text
    assert r.headers["content-type"].startswith("text/html")
    # raw md is not served (the sandbox has the real file at the same path)
    assert not r.text.startswith("---\n")


def test_public_doc_not_found_and_traversal(client: TestClient) -> None:
    for addr in ("/存在しない", f"{SAFETY}/存在しない.md", "/../../pyproject.toml",
                 "/%2e%2e/%2e%2e/pyproject.toml", f"{SAFETY}/index.html", f"{SAFETY}.html",
                 f"{D}{SAFETY}/"):  # a doubled prefix is not accepted
        r = client.get(f"{D}{addr}")
        assert r.status_code == 404, addr
        if ".." not in addr and "%2e" not in addr:  # the client normalizes `..` and hits another route
            assert "content-security-policy" in r.headers


def test_old_cage_urls_are_gone(client: TestClient) -> None:
    """The old `/ext/cage/…` has no alias (switched before going to production)."""
    for u in (f"/ext/cage/doc{SAFETY}", "/ext/cage/static/x.pdf"):
        assert client.get(u).status_code == 404, u


def test_public_doc_head_and_304(client: TestClient) -> None:
    u = f"{D}{SAFETY}/"
    g = client.get(u)
    h = client.head(u)
    assert h.status_code == 200 and h.content == b""
    assert h.headers["content-length"] == g.headers["content-length"]
    assert h.headers["etag"] == g.headers["etag"]
    r = client.get(u, headers={"If-None-Match": g.headers["etag"]})
    assert r.status_code == 304 and r.content == b""


def test_public_doc_heading_ids_match_read_doc(client: TestClient) -> None:
    """HTML heading ids equal the heading text as it appears in the read_doc body."""
    path = _node_with(client, "## ")
    body = client.post("/doc/read", json={"path": path}).text
    first = next(l for l in body.splitlines() if l.startswith("## "))[3:].strip()
    page = client.get(path).text
    if "*" not in first and "`" not in first:
        assert f'id="{first}"' in page.replace("&amp;", "&")


def _static_file(ext: str) -> str:
    files = sorted(server.STATIC_ROOT.rglob(f"*.{ext}"))
    if not files:
        pytest.skip(f"no .{ext} in static")
    return files[0].relative_to(server.STATIC_ROOT).as_posix()


def test_public_static_types_range_and_304(client: TestClient) -> None:
    rel = _static_file("pdf")
    url = f"{STATIC_PREFIX}/{rel}"
    assert url.startswith("/ext/doc-static/")
    r = client.get(url)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["accept-ranges"] == "bytes"
    assert r.headers["x-content-type-options"] == "nosniff"
    size = int(r.headers["content-length"])
    p = client.get(url, headers={"Range": "bytes=0-9"})
    assert p.status_code == 206
    assert p.content == r.content[:10]
    assert p.headers["content-range"] == f"bytes 0-9/{size}"
    h = client.head(url)
    assert h.status_code == 200 and h.content == b""
    assert int(h.headers["content-length"]) == size
    nm = client.get(url, headers={"If-None-Match": r.headers["etag"]})
    assert nm.status_code == 304
    img = client.get(f"{STATIC_PREFIX}/{_static_file('jpg')}")
    assert img.headers["content-type"] == "image/jpeg"


def test_public_static_traversal_is_rejected(client: TestClient) -> None:
    S = STATIC_PREFIX
    for u in (f"{S}/../../pyproject.toml", f"{S}/%2e%2e/%2e%2e/pyproject.toml",
              f"{S}/..%2f..%2fpyproject.toml"):
        r = client.get(u)
        assert r.status_code == 404, u


# ---------------------------------------------------------------------------
# address round trip: tool path = sandbox path = page URL = citation (one string)
# ---------------------------------------------------------------------------


def _addr_ok(client: TestClient, addr: str) -> None:
    """A canonical address becomes a page on the public route and maps to a real file in the build output (what the sandbox sees)."""
    assert addr.startswith(f"{D}/"), addr
    assert "%" not in addr  # Japanese kept as is (not percent-encoded)
    r = client.get(addr, follow_redirects=False)
    assert r.status_code == 200, addr
    assert r.headers["content-type"].startswith("text/html")
    rel = addr[len(D) + 1:]
    f = server.DOC_OUT_ROOT / rel
    if addr.endswith("/"):  # directory: the same path is a directory in the sandbox, body in _index.*
        assert f.is_dir(), addr
        assert any((f / n).is_file() for n in ("_index.md", "_index.yaml")) or addr == f"{D}/"
    else:  # leaf: the real file in the sandbox itself
        assert addr.endswith(".md") and f.is_file(), addr


def test_list_paths_are_canonical_for_node_and_children(client: TestClient) -> None:
    data = client.post("/doc/list", json={"path": "/診療ガイドライン/肺癌2025_非小細胞癌"}).json()
    assert data["path"] == f"{D}/診療ガイドライン/肺癌2025_非小細胞癌/"
    assert "url" not in data
    _addr_ok(client, data["path"])
    assert data["children"]
    for c in data["children"]:
        assert "url" not in c
        assert c["path"].endswith("/" if c["child_count"] else ".md"), c["path"]
    _addr_ok(client, data["children"][0]["path"])


def test_list_paths_at_root(client: TestClient) -> None:
    data = client.post("/doc/list", json={}).json()
    assert data["path"] == f"{D}/"
    _addr_ok(client, data["path"])
    for c in data["children"]:  # categories (directories) open too
        _addr_ok(client, c["path"])


def test_grep_match_paths_are_canonical(client: TestClient) -> None:
    r = client.post("/doc/grep", json={"query": "オシメルチニブ", "path": "/", "limit": 5})
    d = r.json()
    assert d["path"] == f"{D}/"
    assert d["matches"]
    for m in d["matches"]:
        assert "url" not in m
        _addr_ok(client, m["path"])


def test_read_returns_canonical_path_header(client: TestClient) -> None:
    from urllib.parse import unquote

    leaf = f"{D}/診療ガイドライン/肺癌2025_非小細胞癌/CQ47.md"
    r = client.post("/doc/read", json={"path": leaf})
    assert r.status_code == 200
    addr = unquote(r.headers["x-doc-path"])
    assert addr == leaf
    _addr_ok(client, addr)
    # the sandbox file body and the read_doc body are the same
    f = (server.DOC_OUT_ROOT / leaf[len(D) + 1:]).read_text(encoding="utf-8")
    assert r.text in f


@pytest.mark.parametrize(
    "given",
    [
        f"{D}/診療ガイドライン/肺癌2025_非小細胞癌/CQ47.md",
        f"{D}/診療ガイドライン/肺癌2025_非小細胞癌/CQ47",
        "/診療ガイドライン/肺癌2025_非小細胞癌/CQ47",
        "/診療ガイドライン/肺癌2025_非小細胞癌/CQ47.md",
    ],
)
def test_leaf_input_forms_are_the_same_node(client: TestClient, given: str) -> None:
    """Input is lenient (with/without prefix and .md); output is the single canonical form."""
    canon = f"{D}/診療ガイドライン/肺癌2025_非小細胞癌/CQ47.md"
    from urllib.parse import unquote

    r = client.post("/doc/read", json={"path": given})
    assert r.status_code == 200, given
    assert unquote(r.headers["x-doc-path"]) == canon
    assert client.post("/doc/list", json={"path": given}).json()["path"] == canon


@pytest.mark.parametrize(
    "given",
    [
        f"{D}/診療ガイドライン/肺癌2025_非小細胞癌/",
        f"{D}/診療ガイドライン/肺癌2025_非小細胞癌",
        f"{D}/診療ガイドライン/肺癌2025_非小細胞癌/_index.md",
        f"{D}/診療ガイドライン/肺癌2025_非小細胞癌.md",
        "/診療ガイドライン/肺癌2025_非小細胞癌",
        "/診療ガイドライン/肺癌2025_非小細胞癌/",
    ],
)
def test_dir_input_forms_are_the_same_node(client: TestClient, given: str) -> None:
    canon = f"{D}/診療ガイドライン/肺癌2025_非小細胞癌/"
    assert client.post("/doc/list", json={"path": given}).json()["path"] == canon
    g = client.post("/doc/grep", json={"query": "オシメルチニブ", "path": given}).json()
    assert g["path"] == canon


@pytest.mark.parametrize("given", ["/", "", "*", "/*", f"{D}/", D, f"{D}/*"])
def test_root_input_forms(client: TestClient, given: str) -> None:
    g = client.post("/doc/grep", json={"query": "オシメルチニブ", "path": given, "limit": 1})
    assert g.json()["path"] == f"{D}/", given


def test_not_found_valid_path_is_canonical(client: TestClient) -> None:
    r = client.post("/doc/read", json={"path": f"{D}/診療ガイドライン/存在しない.md"})
    assert r.status_code == 404
    d = r.json()
    assert d["missing"] == "存在しない"
    assert d["valid_path"] == f"{D}/診療ガイドライン/"
    # the old URL prefix is not an alternative spelling of the same node (not accepted)
    r = client.post("/doc/read", json={"path": f"/ext/cage/doc{SAFETY}"})
    assert r.status_code == 404
    assert r.json()["valid_path"] == f"{D}/"


def test_every_node_addr_is_a_sandbox_path(client: TestClient) -> None:
    """Every node's canonical address maps 1:1 to the build output (bound at /ext/doc in the sandbox).

    For a leaf `<address>` itself is the file; for a directory `<address>` is the directory and
    the body is `_index.*`. Pinned exhaustively: no translation from address to sandbox path.
    """
    from cage.walker import node_addr

    bad: list[str] = []

    def visit(n) -> None:
        addr = node_addr(n)
        f = server.DOC_OUT_ROOT / addr[len(D) + 1:]
        if n.is_dir:
            ok = addr.endswith("/") and f.is_dir()
        else:
            ok = addr.endswith(".md") and f.is_file()
        if not ok:
            bad.append(addr)
        for c in n.children.values():
            visit(c)

    visit(client.app.state.tree)
    assert not bad, bad[:10]


def test_fragment_and_index_yaml_and_static_inputs(client: TestClient) -> None:
    """A citation with `#…` and a directory's `_index.yaml` resolve to the same node. Figures / originals are not doc nodes."""
    leaf = f"{D}/診療ガイドライン/肺癌2025_非小細胞癌/CQ47.md"
    from urllib.parse import unquote

    r = client.post("/doc/read", json={"path": leaf + "#推奨"})
    assert r.status_code == 200 and unquote(r.headers["x-doc-path"]) == leaf
    r = client.post("/doc/read", json={"path": leaf + "#:~:text=オシメルチニブ"})
    assert r.status_code == 200
    assert client.post("/doc/list", json={"path": f"{D}/_index.yaml"}).json()["path"] == f"{D}/"
    r = client.post("/doc/read", json={"path": "/ext/doc-static/x/a.pdf#page=3"})
    assert r.status_code == 400 and r.json()["error"] == "static_path"
