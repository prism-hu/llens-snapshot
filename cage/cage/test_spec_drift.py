"""Checks that hand-copied grep spec numbers have not drifted.

The single source of truth for `GREP_DEFAULT_LIMIT` / `GREP_MAX_LIMIT` is `walker.py`, and
`server.py` imports it, so it cannot drift. The following two, however, **copy the numbers**
and therefore can:

- `nucllei/tools/doc.py` — the tool placed on the nucllei runtime side. It cannot import cage
  (separate process / deployment), so it has to hold the defaults as literals
- the md the model reads (system prompt / doc-* skills) and cage/README.md

Changing only one side yields a silent inconsistency: the model is told "top 10" while the
actual count differs. This file cross-checks them mechanically.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from cage.walker import GREP_DEFAULT_LIMIT, GREP_MAX_LIMIT

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL = REPO_ROOT / "nucllei" / "tools" / "doc.py"

# md files that state the default count in their body. Wording may differ, but the number must match.
# **doc-* skills are picked up by glob** — when they were listed by hand, the later-added
# doc-infection-control / doc-his-faq were missed.
DOCS_WITH_DEFAULT = [
    REPO_ROOT / "cage" / "README.md",
    # The design notes also carry a copy of the constants (the trailing "single source of truth" block).
    # Currently only names, no numbers, but this stops drift the day a number is added.
    REPO_ROOT / "docs" / "cage" / "05_rag_overview.md",
    REPO_ROOT / "nucllei" / "prompts" / "llens-thinking.md",
    *sorted((REPO_ROOT / "nucllei" / "skills").glob("doc-*.md")),
]


def _grep_doc_node() -> ast.FunctionDef | ast.AsyncFunctionDef:
    """Extract grep_doc of the nucllei-side tool via AST (no import).

    The tool depends on requests / pydantic and assumes the nucllei runtime, so it is not
    imported in cage's test environment.
    """
    tree = ast.parse(TOOL.read_text(encoding="utf-8"))
    cls = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Tools"
    )
    return next(
        n
        for n in cls.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "grep_doc"
    )


def _grep_doc_defaults() -> dict[str, object]:
    """Default values of grep_doc's arguments."""
    fn = _grep_doc_node()
    args = fn.args.args[1:]  # drop self
    defaults = fn.args.defaults
    pairs = dict(zip([a.arg for a in args[-len(defaults) :]], defaults))
    return {k: ast.literal_eval(v) for k, v in pairs.items()}


@pytest.mark.skipif(not TOOL.exists(), reason="nucllei tool is not checked out alongside")
def test_tool_default_limit_matches_walker() -> None:
    defaults = _grep_doc_defaults()
    assert defaults["limit"] == GREP_DEFAULT_LIMIT, (
        f"grep_doc(limit={defaults['limit']}) in nucllei/tools/doc.py differs from "
        f"walker.GREP_DEFAULT_LIMIT={GREP_DEFAULT_LIMIT}"
    )
    assert defaults["offset"] == 0


@pytest.mark.skipif(not TOOL.exists(), reason="nucllei tool is not checked out alongside")
def test_tool_docstring_states_current_bounds() -> None:
    """The docstring is the only spec the model reads, so its max / default numbers must match the implementation.

    The subject is **the grep_doc docstring itself**. An `in` check against the whole file would
    pass merely because a comment or another method has the same number, making the drift check
    a no-op.
    """
    doc = ast.get_docstring(_grep_doc_node())
    assert doc is not None, "grep_doc docstring is gone (the model cannot read the spec)"
    assert f"1〜{GREP_MAX_LIMIT}" in doc, "docstring limit maximum differs from the implementation"
    assert f"既定 {GREP_DEFAULT_LIMIT}" in doc, "docstring default count differs from the implementation"


@pytest.mark.skipif(not TOOL.exists(), reason="nucllei tool is not checked out alongside")
def test_tool_docstring_describes_the_loose_fallback() -> None:
    """Does the docstring mention loose mode and its extra keys?

    Without this, the model misreads results found by word segmentation as "the query appears
    literally in the body" and builds citations on that. In medical use this is the costliest
    kind of writing accident, so it is guarded mechanically.
    """
    doc = ast.get_docstring(_grep_doc_node())
    assert doc is not None
    for key in ("mode", "terms", "absent_terms", "ignored_terms", "note", "matched_terms"):
        assert key in doc, f"grep_doc docstring does not describe response key {key!r}"
    assert "exact" in doc and "loose" in doc, "the two mode values are not in the docstring"
    assert "read_doc" in doc, "the instruction to verify loose results with read_doc is gone from the docstring"


@pytest.mark.parametrize("path", DOCS_WITH_DEFAULT, ids=lambda p: p.name)
def test_docs_state_the_same_default(path: Path) -> None:
    """Do the md files state a different count? Fails if the N in "N 件" differs from the default."""
    if not path.exists():
        pytest.skip(f"{path} is missing")
    text = path.read_text(encoding="utf-8")
    # Two spellings are picked up:
    #   1. 「上位 10 件」「既定 10 件」— skills and the system prompt use this form
    #   2. 「`limit` 既定 10 / 最大 50」— cage/README.md only uses this form; the old pattern
    #      required 「件」, matched nothing, and so **passed unconditionally**
    #      (changing the README's number kept it green)
    # Form 2 is restricted to the vicinity of limit: the same README has an unrelated
    # 「(既定 20000)」, which a naive 「既定 N」 match would grab as a false positive.
    found = {int(m) for m in re.findall(r"(?:上位|既定)\s*(\d+)\s*件", text)}
    found |= {
        int(m)
        for m in re.findall(
            r"limit[^\n]{0,40}?(?:上位|既定)\s*(?:`[^`]+`\s*=\s*)?(\d+)", text, re.I
        )
    }
    assert not (found - {GREP_DEFAULT_LIMIT}), (
        f"{path.name} states a count different from the default: {sorted(found)} "
        f"(walker.GREP_DEFAULT_LIMIT={GREP_DEFAULT_LIMIT})"
    )
    # The **maximum** is copied into the same md too (`cage/README.md`: 「`limit` 既定 10 / 最大 50」
    # 「上限 `GREP_MAX_LIMIT` = 50」). The old pattern only looked at 「上位|既定」, so changing
    # just the maximum kept it green. Restricted to the vicinity of `limit` because the same
    # README lists unrelated maxima (500,000 rows, `since` 30d, `max_output_chars`).
    bounds = {
        int(m)
        for m in re.findall(
            r"limit[^\n]{0,60}?(?:上限|最大)\s*(?:`[^`]+`\s*=\s*)?(\d+)", text, re.I
        )
    }
    assert not (bounds - {GREP_MAX_LIMIT}), (
        f"{path.name} states a count different from the maximum: {sorted(bounds)} "
        f"(walker.GREP_MAX_LIMIT={GREP_MAX_LIMIT})"
    )


# --- hand-copied coverage statements (2026-09-05) --------------------------------
# The same class of problem as the "counts" above also hit the **coverage**. When 診療報酬
# (fee schedule) was extended from chapter 1 only to all chapters, the model-facing text lived in
# 5 places (category list in the system prompt / doc-shinryo-hoshu skill / 2 nodes of the LLENS
# manual / docs/model-scope.md) and only 2 were updated. The rest kept saying "chapter 2
# (特掲診療料) is not included", answering `胸腔穿刺は何点` with "not covered" (`J008` exists).
#
# This is an accident of the **"answer 'absent' for something present"** kind, more harmful than
# a count drift, and it is mechanically detectable from the doc tree. The tests below catch
# "categories / sections changed but the text did not follow" in both directions.
#
# The LLENS manual side (md inside the doc tree) is **also checked here**. `cage/data` is
# outside git (delivered on media), but on machines where the symlink target exists `isdir()` is
# True and every data-dependent test below actually runs under the same
# `skipif(not DOC_DATA_ROOT.is_dir())`. On machines without data (CI, right after delivery) they
# are simply skipped, so the copies inside the doc tree
# (`05_LLENS固有の機能/03_収録している文書` `04_収録していない情報`) are checked under the same condition.

DATA_ROOT = REPO_ROOT / "cage" / "data"
DOC_DATA_ROOT = DATA_ROOT / "doc"
MODEL_FACING_DOCS = [
    REPO_ROOT / "nucllei" / "prompts" / "llens-thinking.md",
    *sorted((REPO_ROOT / "nucllei" / "skills").glob("doc-*.md")),
]


def _top_level_categories() -> list[str]:
    """Names of the doc tree's top level (= one corpus group each)."""
    return sorted(p.name for p in DOC_DATA_ROOT.iterdir() if p.is_dir())


@pytest.mark.skipif(
    not DOC_DATA_ROOT.is_dir(), reason="cage/data is outside git (delivered on media)"
)
def test_every_corpus_is_named_somewhere_the_model_reads() -> None:
    """Never have a category that is included but named nowhere in model-facing text.

    If a category is added but neither the system prompt list nor any doc-* skill names it,
    the model does not know it exists = including it was pointless.
    """
    texts = {p: p.read_text(encoding="utf-8") for p in MODEL_FACING_DOCS if p.exists()}
    unnamed = [
        cat
        for cat in _top_level_categories()
        if not any(cat in t for t in texts.values())
    ]
    assert not unnamed, (
        f"categories present in doc but named nowhere in model-facing text: {unnamed} "
        f"(checked: {[p.name for p in texts]})"
    )


def _assert_skill_matches_sections(
    skill_name: str, doc_prefix: str, *, include_leaves: bool = False
) -> None:
    """Cross-check the skill's path table against the real sections under the prefix, **both ways**.

    - exists but not listed → answers "absent" for something present (what happened with 診療報酬)
    - listed but does not exist → claims something absent is present and induces wrong paths

    `doc_prefix` is like `/診療報酬` or `/診療ガイドライン/神経疾患`: **with a leading `/`, up to
    the parent directory**. Only the one level directly below is checked (deeper paths in the
    text only contribute their first segment).

    With `include_leaves=True`, direct **leaves (`.md` / `.yaml`), extension stripped**, are
    included too. In layouts like DPC where "common-rule nodes sit as leaves directly below",
    looking at directories only would miss key nodes such as `00_算定方法`.
    """
    skill = REPO_ROOT / "nucllei" / "skills" / skill_name
    if not skill.exists():
        pytest.skip(f"{skill_name} is missing")
    root = DOC_DATA_ROOT.joinpath(*doc_prefix.strip("/").split("/"))
    if not root.is_dir():
        pytest.skip(f"{doc_prefix} corpus is not included")
    doc_prefix = "/ext/doc" + doc_prefix  # the text uses the canonical form (`/ext/doc/…`)
    text = skill.read_text(encoding="utf-8")
    names = [p.name for p in root.iterdir() if p.is_dir()]
    if include_leaves:
        names += [
            p.stem
            for p in root.iterdir()
            if p.is_file() and p.suffix in (".md", ".yaml") and p.stem != "_index"
        ]
    actual = sorted(names)
    missing = [s for s in actual if s not in text]
    # collect section names the skill mentions and find those that do not exist
    # the text also has the form `grep_doc(path="/診療報酬/…")`, so closing quotes / parens are excluded
    claimed = set(re.findall(re.escape(doc_prefix) + r"/([^\s`|/\"')\]]+)", text))
    phantom = sorted(c for c in claimed if c not in actual)
    assert not missing and not phantom, (
        f"{skill_name} and the doc tree ({doc_prefix}) disagree — "
        f"existing sections missing from the skill: {missing} / sections named by the skill that do not exist: {phantom}"
    )


@pytest.mark.skipif(
    not DOC_DATA_ROOT.is_dir(), reason="cage/data is outside git (delivered on media)"
)
def test_shinryo_hoshu_skill_lists_exactly_the_sections_that_exist() -> None:
    _assert_skill_matches_sections("doc-shinryo-hoshu.md", "/診療報酬")


@pytest.mark.skipif(
    not DOC_DATA_ROOT.is_dir(), reason="cage/data is outside git (delivered on media)"
)
def test_neurology_skill_lists_exactly_the_guidelines_that_exist() -> None:
    """27 神経疾患 (neurology) directories ↔ the `doc-neurology` skill's path table.

    Editions differ per guideline and the model is told to **always answer with the year**.
    Adding a guideline without listing it in the skill yields "not included"; renaming an id
    while the skill keeps the old name hits a nonexistent path. Both are caught mechanically.
    """
    _assert_skill_matches_sections("doc-neurology.md", "/診療ガイドライン/神経疾患")


@pytest.mark.skipif(
    not DOC_DATA_ROOT.is_dir(), reason="cage/data is outside git (delivered on media)"
)
def test_dermatology_skill_lists_exactly_the_guidelines_that_exist() -> None:
    """61 皮膚疾患 (dermatology) directories ↔ the `doc-dermatology` skill's path table.

    Many confusable siblings (`血管炎・血管障害2016` vs `皮膚血管炎・血管障害2023`,
    `頭部血管肉腫` vs `皮膚血管肉腫2025`). One row missing from the table yields "not
    included"; renaming an id while the skill keeps the old name hits an old edition's or a
    different document's path.
    """
    _assert_skill_matches_sections("doc-dermatology.md", "/診療ガイドライン/皮膚疾患")


@pytest.mark.skipif(
    not DOC_DATA_ROOT.is_dir(), reason="cage/data is outside git (delivered on media)"
)
def test_cancer_registry_skill_lists_exactly_the_sources_that_exist() -> None:
    """3 がん登録 (cancer registry) sources ↔ the `doc-cancer-registry` skill's path table.

    The edition is chosen by diagnosis date (hospital 2016 edition / 2006 interpretation volume /
    national notification manual 2026), so one missing source yields "that year's form is not
    included".
    """
    _assert_skill_matches_sections("doc-cancer-registry.md", "/がん登録")


@pytest.mark.skipif(
    not DOC_DATA_ROOT.is_dir(), reason="cage/data is outside git (delivered on media)"
)
def test_dpc_skill_lists_exactly_the_sections_that_exist() -> None:
    """Directly under the DPC table (18 MDC directories + `00_*` common rules) ↔ the `doc-dpc` skill.

    The MDC is the first 2 of the 14 digits, so for an MDC the skill does not name the model
    cannot tell which directory to look in. The `00_*` leaves (calculation method, coefficients,
    special inpatient fee add-ons, etc.) are checked via `include_leaves` for the same reason.
    """
    _assert_skill_matches_sections(
        "doc-dpc.md", "/診療報酬/DPC点数表R08", include_leaves=True
    )


CLINICAL_SCALES = DOC_DATA_ROOT / "臨床スケール"


def _clinical_scale_leaves() -> list[Path]:
    return sorted(
        p for p in CLINICAL_SCALES.rglob("*.md") if p.name != "_index.md"
    )


@pytest.mark.skipif(
    not CLINICAL_SCALES.is_dir(), reason="/臨床スケール not included (cage/data is delivered on media)"
)
def test_clinical_scales_skill_lists_every_node() -> None:
    """Every leaf of `/臨床スケール` ↔ the `doc-clinical-scales` skill's index (literal paths).

    The skill body is an index; the model passes its paths to `read_doc` verbatim. A node added
    but missing from the index gets answered as "not included" and the scale is computed from
    memorized scoring (exactly what this skill is meant to prevent). The reverse direction (in
    the index but nonexistent) is covered by `test_model_facing_paths_all_resolve`.
    """
    skill = REPO_ROOT / "nucllei" / "skills" / "doc-clinical-scales.md"
    text = skill.read_text(encoding="utf-8")
    missing = [
        "/ext/doc/" + p.relative_to(DOC_DATA_ROOT).as_posix()
        for p in _clinical_scale_leaves()
    ]
    missing = [m for m in missing if f"`{m}`" not in text]
    assert not missing, f"nodes missing from the doc-clinical-scales skill index: {missing}"


@pytest.mark.skipif(
    not CLINICAL_SCALES.is_dir(), reason="/臨床スケール not included (cage/data is delivered on media)"
)
def test_clinical_scale_nodes_carry_source_and_worked_example() -> None:
    """Every node carries a source and a worked example (for self-verification).

    The skill's rule is "read the node → compute with run_python → check your code against the
    node's worked example". A node without one silently breaks that rule.
    """
    bad = []
    for p in _clinical_scale_leaves():
        body = p.read_text(encoding="utf-8")
        lacks = [h for h in ("## 出典", "## 計算例") if h not in body]
        if lacks:
            bad.append(f"{p.relative_to(CLINICAL_SCALES)}: {lacks}")
    assert not bad, f"nodes lacking the 出典 / 計算例 sections: {bad}"


# --- checks on the model-facing text itself (2026-09-12 review T1/T2/T5/T6/T7) --------
# From here on this is not a cross-check against the doc tree but a mechanical check of **what
# the md the model reads claims about itself**. Skills are written to be "obvious on reading",
# so drifted paths, quantities and cross-references slip past human review.


def _model_facing_texts() -> dict[Path, str]:
    return {p: p.read_text(encoding="utf-8") for p in MODEL_FACING_DOCS if p.exists()}


_BACKTICK = re.compile(r"`([^`\n]+)`")
# Paths with placeholders are not literal (`<部>` `…` `*`). Skills only show them in a
# "do not build paths like this" context, so they are excluded from the existence check.
_PLACEHOLDER_CHARS = "<>{}…*"


# Collect doc paths the model-facing text mentions. **Not only inside backticks** — tables and
# prose also have the form `grep_doc(path="/ext/doc/…")`. Canonical form starts with `/ext/doc/` (2026-10-06).
# Node names may contain `(` `)` `、` (`01_検査・輸血部(検査部門)`), so only an unmatched trailing `)`
# (closing a markdown link) is stripped. `、` counts as part of a name only inside backticks.
_DOC_ADDR = re.compile(r"/ext/doc/[^\s`|\"'\]、。,]*")
_DOC_ADDR_IN_TICKS = re.compile(r"`(/ext/doc/[^`]*)`")


def _addr_target(token: str) -> tuple[str, str]:
    """`/ext/doc/A/B.md#section` → (form, relative path in the data). Form is leaf / dir / bare."""
    token = token.split("#", 1)[0]
    rel = token[len("/ext/doc/"):]
    if rel.endswith(".md"):
        return "leaf", rel[: -len(".md")]
    if rel == "" or rel.endswith("/"):
        return "dir", rel.rstrip("/")
    return "bare", rel


def _model_facing_all_texts() -> dict[Path, str]:
    """All model-facing text that may contain doc paths (all prompts + all skills; retired excluded)."""
    files = [
        *sorted((REPO_ROOT / "nucllei" / "prompts").glob("*.md")),
        *sorted((REPO_ROOT / "nucllei" / "skills").glob("*.md")),
    ]
    return {p: p.read_text(encoding="utf-8") for p in files}


@pytest.mark.skipif(
    not DOC_DATA_ROOT.is_dir(), reason="cage/data is outside git (delivered on media)"
)
def test_model_facing_paths_all_resolve() -> None:
    """Every doc path named by prompts / skills exists, in canonical form (T1).

    The model passes paths written in skills verbatim to `read_doc` / `grep_doc` and copies them
    into citations. If an id was renamed but the skill kept the old name, the model answers
    "not included" (one case occurred, of the form `91_別表1.md`).

    A doc path is a single string (2026-10-06): leaf `/ext/doc/…/<name>.md`, directory
    `/ext/doc/…/` (trailing `/`). The tool also accepts other spellings (no prefix, no `.md`),
    but if the text writes another form the model copies it into citations too (the citation
    is then counted as a different address), so the text uses the canonical form only. The old
    form (`/<category>/…` in backticks) also fails.
    """
    tops = tuple("/" + c for c in _top_level_categories())
    bad_form: list[str] = []
    missing: list[str] = []
    old_form: list[str] = []
    for path, text in _model_facing_all_texts().items():
        for lineno, line in enumerate(text.splitlines(), 1):
            for m in _BACKTICK.finditer(line):
                if m.group(1).strip().startswith(tops):
                    old_form.append(f"{path.name}:{lineno} {m.group(1)}")
            ticked = [(m.start(1), m.group(1)) for m in _DOC_ADDR_IN_TICKS.finditer(line)]
            spans = [(st, st + len(t)) for st, t in ticked]
            loose = [(m.start(), m.group(0)) for m in _DOC_ADDR.finditer(line)
                     if not any(a <= m.start() < b for a, b in spans)]
            for _st, raw in ticked + loose:
                if any(c in raw for c in _PLACEHOLDER_CHARS):
                    continue
                token = raw.split('"', 1)[0].rstrip(".:")
                while token.endswith(")") and token.count(")") > token.count("("):
                    token = token[:-1].rstrip(".:")
                where = f"{path.name}:{lineno} {token}"
                form, rel = _addr_target(token)
                fs = DOC_DATA_ROOT.joinpath(*rel.split("/")) if rel else DOC_DATA_ROOT
                is_dir = fs.is_dir()
                # `with_suffix` is not used: for ids containing `.` (`MELD3.0` `RECIST1.1`) it
                # misreads the tail as an extension (2026-10-04)
                is_leaf = fs.with_name(fs.name + ".md").is_file() if rel else False
                if not (is_dir or is_leaf):
                    missing.append(where)
                elif form != ("dir" if is_dir else "leaf"):
                    bad_form.append(where)
    assert not bad_form and not missing and not old_form, (
        f"doc paths in model-facing text — not canonical (leaf: .md, directory: trailing /): {bad_form} / "
        f"nonexistent: {missing} / old form (no /ext/doc/): {old_form}"
    )


@pytest.mark.skipif(
    not DOC_DATA_ROOT.is_dir(), reason="cage/data is outside git (delivered on media)"
)
def test_every_section_is_named_somewhere_the_model_reads() -> None:
    """Every category's **direct sections** are named somewhere in model-facing text (T2).

    One level deeper than `test_every_corpus_is_named_somewhere_the_model_reads`. A per-category
    check passes on "`/診療ガイドライン` is named" and cannot catch the (actually observed) case
    where 皮膚疾患 was added below it and nobody mentioned it.

    Which skill should mention it is not decided here (`/院内マニュアル` `/診療ガイドライン` are
    1 category = several skills). Only what is **named nowhere** fails.
    """
    texts = _model_facing_texts()
    unnamed: list[str] = []
    for cat in _top_level_categories():
        for section in sorted(
            p.name for p in (DOC_DATA_ROOT / cat).iterdir() if p.is_dir()
        ):
            if not any(section in t for t in texts.values()):
                unnamed.append(f"/{cat}/{section}")
    assert not unnamed, (
        f"sections present in doc but named nowhere in model-facing text: {unnamed} "
        f"(checked: {[p.name for p in texts]})"
    )


# Pairs of skills that split one category. Normally **only one of them is loaded**, so unless
# each names the other, the model stops at "not in this corpus"
# (these 6 pairs get the most boundary-crossing questions).
SPLIT_SKILL_PAIRS = [
    ("doc-lung-cancer", "doc-neurology"),
    ("doc-lung-cancer", "doc-dermatology"),
    ("doc-neurology", "doc-dermatology"),
    ("doc-safety-manual", "doc-infection-control"),
    ("doc-shinryo-hoshu", "doc-dpc"),
    ("doc-his-faq", "doc-llens-manual"),
]


@pytest.mark.parametrize("a,b", SPLIT_SKILL_PAIRS, ids=lambda v: v)
def test_split_skills_name_each_other(a: str, b: str) -> None:
    """Split skills name each other (T6)."""
    skills = REPO_ROOT / "nucllei" / "skills"
    pa, pb = skills / f"{a}.md", skills / f"{b}.md"
    if not (pa.exists() and pb.exists()):
        pytest.skip(f"{a} / {b} are not both present")
    ta, tb = pa.read_text(encoding="utf-8"), pb.read_text(encoding="utf-8")
    missing = [f"{x} does not name {y}" for x, y, t in ((a, b, ta), (b, a, tb))
               if y not in t]
    assert not missing, "; ".join(missing)


# Words never shown to the model (developer codenames, pipeline terms). Documents are called "doc".
FORBIDDEN_WORDS = ["棚", "overlay", "manifest", "ingest", "VLM", "scratchpad"]
# `cage` never appears in model-facing text. Until 2026-10-06 it was allowed only in deploy
# paths (`/ext/cage/…`), but since doc addresses became `/ext/doc/…` and figures / originals
# `/ext/doc-static/…`, there is no exception left (the sandbox `/mnt/cage` is gone too).
CAGE_ALLOWED_PREFIXES: list[str] = []


@pytest.mark.parametrize("word", FORBIDDEN_WORDS)
def test_model_facing_docs_avoid_internal_jargon(word: str) -> None:
    """No developer-side words leak into model-facing text (T5)."""
    hits = [
        f"{path.name}:{lineno}"
        for path, text in _model_facing_texts().items()
        for lineno, line in enumerate(text.splitlines(), 1)
        if word in line
    ]
    assert not hits, f"{word!r} remains in model-facing text: {hits}"


def test_model_facing_docs_never_say_cage() -> None:
    """The word `cage` does not appear in model-facing text (all prompts, all skills) (T5)."""
    hits: list[str] = []
    for path, text in _model_facing_all_texts().items():
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line
            for allowed in CAGE_ALLOWED_PREFIXES:
                stripped = stripped.replace(allowed, "")
            if "cage" in stripped:
                hits.append(f"{path.name}:{lineno}")
    assert not hits, (
        f"'cage' remains in model-facing text outside deploy paths: {hits} "
        f"(only {CAGE_ALLOWED_PREFIXES} are allowed)"
    )


# The two LLENS manual nodes on "what is / is not included". The system prompt routes
# "when asked for the breakdown, read_doc these 2 nodes and answer", so if they are stale a path
# opens to **answer "not included" for something present** (happened with がん登録 and 皮膚疾患).
LLENS_INVENTORY_NODES = [
    DOC_DATA_ROOT
    / "医療情報システム/LLENSマニュアル/05_LLENS固有の機能/03_収録している文書.md",
    DOC_DATA_ROOT
    / "医療情報システム/LLENSマニュアル/05_LLENS固有の機能/04_収録していない情報.md",
]


def _acceptable_names(section: str) -> list[str]:
    """A section name plus the paraphrases a human might write for it.

    Rule: directory names are mechanical ids (`肺癌2025_診断` `CTCAEv5_JCOG_2025`) and do not
    necessarily appear verbatim in prose. The part **before the first digit / `_`** is taken as
    the disease / source name and accepted too (`肺癌2025_診断` → `肺癌`, `CTCAEv5_JCOG_2025` →
    `CTCAE`; trailing ASCII lowercase is an edition marker and is dropped). Names without digits
    (`皮膚疾患` `神経疾患`) are their own name, so no paraphrase is accepted.
    """
    head = re.split(r"[0-9_]", section, maxsplit=1)[0].rstrip("abcdefghijklmnopqrstuvwxyz")
    # For ids starting with digits (`01_…`) head is empty. An empty string is contained in any
    # text and would void the check, so a paraphrase is accepted only when it has 2+ chars.
    if head == section or len(head) < 2:
        return [section]
    return [section, head]


@pytest.mark.skipif(
    not DOC_DATA_ROOT.is_dir(), reason="cage/data is outside git (delivered on media)"
)
@pytest.mark.parametrize("node", LLENS_INVENTORY_NODES, ids=lambda p: p.name)
def test_llens_manual_inventory_covers_the_tree(node: Path) -> None:
    """The 2 inventory nodes mention every top-level category and every direct section
    of 診療ガイドライン (T7)."""
    if not node.exists():
        pytest.skip(f"{node} is missing")
    text = node.read_text(encoding="utf-8")
    wanted = [[c] for c in _top_level_categories()]
    guidelines = DOC_DATA_ROOT / "診療ガイドライン"
    if guidelines.is_dir():
        wanted += [
            _acceptable_names(p.name)
            for p in sorted(guidelines.iterdir())
            if p.is_dir()
        ]
    missing = [names[0] for names in wanted if not any(n in text for n in names)]
    assert not missing, (
        f"included content not mentioned by {node.name}: {missing} "
        f"(these 2 nodes are the answer path for 'what is included')"
    )


# --- quantity claims in skills ↔ measured values (T3) -----------------------------
# Only the 2 society-guideline corpora share a consistent format, so restrict to them. The
# 診療報酬 coverage table mixes units (section count / leaf count / CSV rows) and is excluded.
QUANTITY_CLAIMS = [
    ("doc-dermatology.md", "/診療ガイドライン/皮膚疾患", "jda_guidelines"),
    ("doc-neurology.md", "/診療ガイドライン/神経疾患", "jns_guidelines"),
]
# lines like 「61 ディレクトリ / 2,798 ノード」「約 2,100 ノード」
_CLAIM_NODES = re.compile(r"ディレクトリ\s*/\s*(約)?\s*([\d,]+)\s*ノード")
# lines like 「図の切り出し画像が 518 点」「図そのものを切り出した画像が 518 点」
_CLAIM_FIGS = re.compile(r"切り出(?:し|した)画像が\s*(約)?\s*([\d,]+)\s*点")
# lines like 「うち 1,468 ページはページ画像から起こした再現テキスト」
_CLAIM_OVERLAY = re.compile(r"(約)?\s*([\d,]+)\s*ページはページ画像から")


def _claims(pattern: re.Pattern[str], text: str) -> list[tuple[bool, int]]:
    return [
        (bool(approx), int(num.replace(",", "")))
        for approx, num in pattern.findall(text)
    ]


def _assert_claims(label: str, claims: list[tuple[bool, int]], actual: int) -> None:
    """Claims match the measured value. With `約` (approx.) a ±5% tolerance is allowed."""
    assert claims, f"the {label} claim is gone from the skill (format may have changed)"
    for approx, claimed in claims:
        if approx:
            assert abs(claimed - actual) <= actual * 0.05, (
                f"{label}: 「約 {claimed:,}」 is off from the measured {actual:,} by more than 5%"
            )
        else:
            assert claimed == actual, f"{label}: claimed {claimed:,} ≠ measured {actual:,}"


@pytest.mark.skipif(
    not DOC_DATA_ROOT.is_dir(), reason="cage/data is outside git (delivered on media)"
)
@pytest.mark.parametrize(
    "skill_name,doc_prefix,corpus", QUANTITY_CLAIMS, ids=lambda v: str(v)
)
def test_skill_counts_match_the_data(
    skill_name: str, doc_prefix: str, corpus: str
) -> None:
    """Node count / figure count / reproduced-page count claimed by the skill match the data (T3).

    The model **changes its behaviour based on these numbers**, e.g. "2,798 nodes, so narrow the
    search". This stops the numbers going stale when more content is ingested.
    """
    skill = REPO_ROOT / "nucllei" / "skills" / skill_name
    root = DOC_DATA_ROOT.joinpath(*doc_prefix.strip("/").split("/"))
    if not (skill.exists() and root.is_dir()):
        pytest.skip(f"{skill_name} / {doc_prefix} are not both present")
    text = skill.read_text(encoding="utf-8")

    # node count = number of md / yaml below − the corpus's own `_index.md`
    nodes = sum(1 for p in root.rglob("*") if p.suffix in (".md", ".yaml")) - 1
    _assert_claims(f"{skill_name} node count", _claims(_CLAIM_NODES, text), nodes)

    figs = DATA_ROOT / "static" / corpus / "figs"
    if figs.is_dir():
        _assert_claims(
            f"{skill_name} figure count",
            _claims(_CLAIM_FIGS, text),
            sum(1 for p in figs.rglob("*") if p.is_file()),
        )

    overlays = DATA_ROOT / "ingest" / corpus / "overlays"
    if overlays.is_dir():
        _assert_claims(
            f"{skill_name} reproduced page count",
            _claims(_CLAIM_OVERLAY, text),
            sum(1 for _ in overlays.rglob("*.md")),
        )
