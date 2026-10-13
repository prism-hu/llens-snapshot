"""Walk data/doc/ and build the knowledge tree in memory.

Node = 1 directory or 1 file (`.md` / `.yaml`).
A directory may carry metadata in `_index.md` / `_index.yaml` (optional).
No limit on depth. The on-disk structure becomes the tree as is.

Each Node's `path` is the full path string from the root (leading `/`):
- root:       "/"
- child:      "/foo"
- grandchild: "/foo/bar"

Validated at startup:
- `_index.md` and `_index.yaml` coexisting at the same level → error
- sibling id collision (`X.md` and `X/` etc.) → error
- front matter parse failure / missing title → error
"""

from __future__ import annotations

import datetime as _dt
import math
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import frontmatter  # python-frontmatter
import yaml


@dataclass
class Node:
    path: str  # full path string from the root. root is "/".
    title: str
    meta: dict[str, Any]
    body: str  # internal only; not exposed via lookup, only via read
    children: dict[str, "Node"] = field(default_factory=dict)
    # Whether the node came from a directory (decides the address form: directory ends with `/`, file with `.md`).
    # Not decided by having children — a childless directory still lives at `X/_index.md` in the sandbox.
    is_dir: bool = False
    # Extension of the leaf's real file (tail of the address). Body-less `.yaml` leaves are not in the
    # current corpus, but if present the address is `X.yaml`, as the real file in the sandbox
    leaf_suffix: str = ".md"
    # NFKC + lowercased text for grep (title, description, body). Built on the first grep and
    # reused thereafter. Safe because the tree is immutable after walk.
    # Re-normalizing every time makes NFKC the dominant cost (measured: grep over root 480ms → 12ms).
    _norm: tuple[str, str, str] | None = field(
        default=None, repr=False, compare=False
    )


class WalkerError(Exception):
    """Startup-time validation error. Propagating means the server should fail to start."""


# ---------------------------------------------------------------------------
# path helpers
# ---------------------------------------------------------------------------


def split_path(path: str) -> list[str]:
    """`/a/b` → ['a','b']. `/` or `''` → []."""
    if path in ("", "/"):
        return []
    if not path.startswith("/"):
        raise ValueError(f"path は / 始まりで指定する (指定: {path})")
    return [p for p in path.split("/")[1:] if p]


def join_path(segments: list[str]) -> str:
    return "/" if not segments else "/" + "/".join(segments)


# ---------------------------------------------------------------------------
# doc address (the one string shown externally)
# ---------------------------------------------------------------------------
#
# A doc address is one string: **tool path = sandbox path = page URL = citation** (<redacted> 2026-10-06):
#   leaf         `/ext/doc/<category>/…/<name>.md`  (same string as the real file in the sandbox.
#                `….yaml` for a body-less `.yaml` leaf — none in the current corpus)
#   directory    `/ext/doc/<category>/…/`           (trailing `/`. Body is `…/_index.md` in the sandbox)
#   root         `/ext/doc/`
# Figures / originals live under a separate root `/ext/doc-static/…` (under doc they would show up in
# list_doc and need special-casing). The sandbox bind, nucllei's /ext mount name and cage's public route
# use the same prefix (docker-compose.yml). It does not change per deployment, so it is not an env var.
#
# Node.path (`/<category>/…`, no extension) stays as the internal key. All traffic in and out goes
# through `node_addr` (output: one canonical form) and `addr_to_path` (input: lenient) only.

DOC_PREFIX = "/ext/doc"
STATIC_PREFIX = "/ext/doc-static"


def node_addr(node: Node) -> str:
    """The node's canonical address (table above). Raw string, not percent-encoded."""
    if node.path == "/":
        return DOC_PREFIX + "/"
    return DOC_PREFIX + node.path + ("/" if node.is_dir else node.leaf_suffix)


def addr_to_path(addr: str) -> str:
    """External address → internal Node.path notation (existence not checked; navigate descends).

    Accepts only alternate spellings of the same node (no guessing):
      - with or without the `/ext/doc` prefix (`/ext/doc/A/B.md` and the old form `/A/B`)
      - with or without a leaf's `.md` (`.yaml` for a `.yaml` leaf), with or without a directory's trailing `/`
      - a directory's body file `…/_index.md` `…/_index.yaml` (the real file of that directory as seen in the sandbox)
      - a citation's location `#heading` / `#:~:text=…` (a position within the page, same node. Names never
        contain `#` — build.PATH_HOSTILE)
    ValueError unless it starts with `/` (same as split_path). Figures / originals (`/ext/doc-static/…`)
    are not doc nodes, so the caller (server) rejects them first.
    """
    s = addr.split("#", 1)[0]
    if s in ("", "/"):
        return "/"
    if not s.startswith("/"):
        raise ValueError(f"path は / 始まりで指定する (指定: {addr})")
    if s == DOC_PREFIX or s.startswith(DOC_PREFIX + "/"):
        s = s[len(DOC_PREFIX):] or "/"
    for idx in ("_index.md", "_index.yaml"):
        if s == "/" + idx or s.endswith("/" + idx):
            s = s[: -len(idx)]
            break
    else:
        if s.endswith((".md", ".yaml")):
            s = s.rsplit(".", 1)[0]
    return join_path(split_path(s))


def _join(parent_path: str, name: str) -> str:
    """Append one segment to parent_path. Handles the root parent ("/") cleanly."""
    return f"/{name}" if parent_path == "/" else f"{parent_path}/{name}"


# ---------------------------------------------------------------------------
# loaders
# ---------------------------------------------------------------------------


def _load_md(path: Path) -> tuple[dict[str, Any], str]:
    try:
        with open(path, encoding="utf-8") as f:
            post = frontmatter.load(f)
    except Exception as e:
        raise WalkerError(f"{path}: front matter parse error: {e}") from e
    return dict(post.metadata), post.content


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception as e:
        raise WalkerError(f"{path}: YAML parse error: {e}") from e
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise WalkerError(f"{path}: top-level YAML must be a mapping")
    return data


# ---------------------------------------------------------------------------
# child schema (`fields:`)
# ---------------------------------------------------------------------------
#
# Reserved keys: `title` (implicitly required, always shown) and `fields` (child schema declaration).
# Neither is put into the node's own `meta`.
#
# `fields:` is a closed schema governing the frontmatter of that node's **direct children**:
#   fields:
#     guideline: str        # required, shown in list
#     source?: str          # required, hidden from list (? = hidden)
# - closed: an undeclared key on a child fails startup
# - all required: a declared key missing on a child fails startup (no node-level optional)
# - `?` suffix: not shown in /doc/list meta (provenance)
# - `fields` omitted = empty schema = children have title only (closed)

_RESERVED = ("title", "fields")
_ALLOWED_TYPES = frozenset({"str", "int", "date", "bool", "list"})
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_schema(raw: Any, source: Path | None) -> dict[str, tuple[str, bool]]:
    """Turn a `fields:` declaration into {field name: (type, hidden)}. None → empty (children have title only).

    An invalid declaration itself (not a mapping / unknown type / duplicate) fails startup.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise WalkerError(f"{source}: 'fields' must be a mapping")
    schema: dict[str, tuple[str, bool]] = {}
    for key, typ in raw.items():
        if not isinstance(key, str):
            raise WalkerError(f"{source}: 'fields' key must be a string: {key!r}")
        hidden = key.endswith("?")
        name = key[:-1] if hidden else key
        if not name:
            raise WalkerError(f"{source}: empty field name in 'fields'")
        if not isinstance(typ, str) or typ not in _ALLOWED_TYPES:
            raise WalkerError(
                f"{source}: field '{name}' has unknown type {typ!r} "
                f"(allowed: {sorted(_ALLOWED_TYPES)})"
            )
        if name in schema:
            raise WalkerError(f"{source}: duplicate field '{name}' in 'fields'")
        schema[name] = (typ, hidden)
    return schema


def _check_type(value: Any, typ: str) -> bool:
    if typ == "str":
        return isinstance(value, str)
    if typ == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if typ == "bool":
        return isinstance(value, bool)
    if typ == "list":
        return isinstance(value, list)
    if typ == "date":
        if isinstance(value, _dt.date):
            return True
        return isinstance(value, str) and bool(_DATE_RE.match(value))
    return False


def _validate_against_schema(
    meta: dict[str, Any], schema: dict[str, tuple[str, bool]], source: Path
) -> None:
    """Validate the node's own data (= meta minus title/fields) against the parent's schema.

    closed + all required + type match. A violation fails startup.
    """
    present = set(meta)
    declared = set(schema)
    missing = declared - present
    if missing:
        raise WalkerError(
            f"{source}: missing required field(s) {sorted(missing)} "
            "(declared by parent 'fields')"
        )
    extra = present - declared
    if extra:
        raise WalkerError(
            f"{source}: unexpected field(s) {sorted(extra)} "
            f"(closed schema; allowed: {sorted(declared) or 'title only'})"
        )
    for name, (typ, _hidden) in schema.items():
        if not _check_type(meta[name], typ):
            raise WalkerError(
                f"{source}: field '{name}' must be {typ}, got {meta[name]!r}"
            )


def _project_listed(
    meta: dict[str, Any], schema: dict[str, tuple[str, bool]]
) -> dict[str, Any]:
    """Drop hidden (`?`) fields from meta (= not shown in /doc/list)."""
    hidden = {name for name, (_typ, h) in schema.items() if h}
    return {k: v for k, v in meta.items() if k not in hidden}


def _build(
    *,
    node_path: str,
    fm: dict[str, Any],
    body: str,
    fallback_title: str,
    source: Path | None,
) -> Node:
    title = fm.get("title")
    if title is None:
        if fm:
            raise WalkerError(f"{source}: front matter missing required 'title'")
        title = fallback_title
    elif not isinstance(title, str):
        raise WalkerError(f"{source}: 'title' must be a string")
    meta = {k: v for k, v in fm.items() if k not in _RESERVED}
    return Node(path=node_path, title=title, meta=meta, body=body)


# ---------------------------------------------------------------------------
# walking
# ---------------------------------------------------------------------------


def _walk_file(path: Path, node_path: str) -> Node:
    if path.suffix == ".md":
        fm, body = _load_md(path)
        return _build(
            node_path=node_path, fm=fm, body=body, fallback_title=path.stem, source=path
        )
    if path.suffix == ".yaml":
        fm = _load_yaml(path)
        node = _build(
            node_path=node_path, fm=fm, body="", fallback_title=path.stem, source=path
        )
        node.leaf_suffix = ".yaml"
        return node
    raise WalkerError(f"{path}: unsupported file extension")


def _walk_dir(path: Path, node_path: str) -> Node:
    index_md = path / "_index.md"
    index_yaml = path / "_index.yaml"
    if index_md.exists() and index_yaml.exists():
        raise WalkerError(
            f"{path}: both _index.md and _index.yaml exist; use one only"
        )

    fallback_title = path.name if node_path != "/" else path.name
    if index_md.exists():
        fm, body = _load_md(index_md)
        index_source: Path | None = index_md
        node = _build(
            node_path=node_path,
            fm=fm,
            body=body,
            fallback_title=fallback_title,
            source=index_md,
        )
    elif index_yaml.exists():
        fm = _load_yaml(index_yaml)
        index_source = index_yaml
        node = _build(
            node_path=node_path,
            fm=fm,
            body="",
            fallback_title=fallback_title,
            source=index_yaml,
        )
    else:
        fm = {}
        index_source = None
        node = Node(path=node_path, title=fallback_title, meta={}, body="")
    node.is_dir = True

    # This dir's `fields:` = closed schema for direct children. If absent, children have title only.
    child_schema = _parse_schema(fm.get("fields"), index_source)

    seen: dict[str, Path] = {}
    for child in sorted(path.iterdir()):
        name = child.name
        if name.startswith("_index.") or name.startswith("."):
            continue
        if child.is_file() and child.suffix not in (".md", ".yaml"):
            continue

        key = child.stem if child.is_file() else child.name
        child_path = _join(node_path, key)
        if child.is_file():
            child_node = _walk_file(child, child_path)
        elif child.is_dir():
            child_node = _walk_dir(child, child_path)
        else:
            continue

        # Validate the child's own data against the parent schema → drop hidden from meta (not listed).
        _validate_against_schema(child_node.meta, child_schema, child)
        child_node.meta = _project_listed(child_node.meta, child_schema)

        if key in seen:
            raise WalkerError(
                f"{path}: sibling id collision '{key}': {seen[key]} vs {child}"
            )
        seen[key] = child
        node.children[key] = child_node

    return node


def walk(data_root: Path) -> Node:
    """Walk the given data root directory and return the tree."""
    if not data_root.is_dir():
        raise WalkerError(f"{data_root}: data root is not a directory")
    return _walk_dir(data_root, node_path="/")


# ---------------------------------------------------------------------------
# navigation / serialization
# ---------------------------------------------------------------------------


def navigate(tree: Node, path: str) -> Node | None:
    """Descend the tree by path string. None if absent."""
    node = tree
    for seg in split_path(path):
        nxt = node.children.get(seg)
        if nxt is None:
            return None
        node = nxt
    return node


# ---------------------------------------------------------------------------
# grep
# ---------------------------------------------------------------------------

# Path spellings synonymous with root ("/" / "" / "*" / "/*" all mean the whole root subtree. The
# canonical `/ext/doc/` is mapped back to root by addr_to_path. `/ext/doc/*` is accepted likewise)
ROOT_PATH_ALIASES = frozenset({"", "/", "*", "/*", "/ext/doc/*"})

GREP_DEFAULT_LIMIT = 10
GREP_MAX_LIMIT = 50
GREP_SNIPPET_RADIUS = 40  # ±40 chars around a body hit

# Relevance score weights. A title hit is strong evidence that "the node is the topic itself", so it
# outweighs the raw body hit count (measured: with weight 5, for "針刺し" 14-1 lost to department
# manuals and the 14-x series was split apart. 10 lines them up. Raising to 25 did not change the order).
# Raw hit counts are used, not density divided by length — density pushes index pages of a few
# hundred chars and small nodes to the top and becomes noise, as confirmed by measurement.
GREP_TITLE_WEIGHT = 10
GREP_DESC_WEIGHT = 5


def _normalize(s: str) -> str:
    """NFKC normalization + lowercase. Absorbs full/half-width and case variation for substring comparison."""
    return unicodedata.normalize("NFKC", s).lower()


def _norm_fields(node: Node) -> tuple[str, str, str]:
    """Return the node's (title, description, body) normalized. Computed once and cached on the node."""
    if node._norm is None:
        desc = node.meta.get("description")
        node._norm = (
            _normalize(node.title),
            _normalize(desc) if isinstance(desc, str) else "",
            _normalize(node.body),
        )
    return node._norm


# Snippets are often markdown-rendered inline on one line on the UI side.
# If structural markers like `---` (HR) or `## ` (heading) remain mid-text after newlines are
# collapsed, the UI breaks, so only the structural markers are stripped and the content kept.
_SNIPPET_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_SNIPPET_FENCE_RE = re.compile(r"`{3,}")
_SNIPPET_HR_RE = re.compile(r"-{3,}")
_SNIPPET_HEADING_RE = re.compile(r"#{1,6}\s+")
_SNIPPET_WS_RE = re.compile(r"\s+")


def _sanitize_snippet(s: str) -> str:
    """Shape a snippet (one-line display) into a form friendly to the UI renderer.

    Only markdown **structural markers** are removed; body information is kept:
      - C0 control chars → space
      - <!--, --> (HTML comment boundaries) → removed (MedDRA codes etc. inside are kept)
      - ``` (fenced code) → ` (inline code interpretation is acceptable)
      - --- (HR) → space
      - ## ... # (ATX heading markers) → removed (heading text is kept)
      - runs of whitespace → collapsed to one
    """
    s = _SNIPPET_CONTROL_RE.sub(" ", s)
    s = s.replace("<!--", "").replace("-->", "")
    s = _SNIPPET_FENCE_RE.sub("`", s)
    s = _SNIPPET_HR_RE.sub(" ", s)
    s = _SNIPPET_HEADING_RE.sub("", s)
    return _SNIPPET_WS_RE.sub(" ", s).strip()


# The read_doc body is markdown-rendered on the OWUI side. When a `---` / `===` line (setext
# underline) follows a body line with no blank line between, marked applies the CommonMark setext
# heading rule and swallows "every line back to the previous blank line" into one <h2>. If an OWUI
# <details> (tool_calls / reasoning) falls inside that range, the HTML gets escaped along with it
# and rendering collapses (details: docs/cage/07_markdown_setext_trap.md).
# Fix: isolate any line that could be a setext underline with blank lines. A blank line before it
# defeats setext, and a blank line after it renders it safely as a standalone <hr>.
# Markdown structure (headings / lists / links / tables) is never touched. Unlike snippets, the body
# must stay readable with its structure, so the point is "only add blank lines", never flatten.
_SETEXT_LINE_RE = re.compile(r"^ {0,3}(?:=+|-+) *$")
_FENCE_LINE_RE = re.compile(r"^ {0,3}(?:`{3,}|~{3,})")


def isolate_thematic_breaks(md: str) -> str:
    """Guarantee blank lines around setext underline (`===` / `---`) lines to prevent heading-ification.

    Nothing is removed from the body; only blank lines are added. Inside fenced code (``` / ~~~)
    is code, so it is exempt (an inner `---` is not broken).
    """
    lines = md.split("\n")
    out: list[str] = []
    in_fence = False
    for i, line in enumerate(lines):
        if _FENCE_LINE_RE.match(line):
            in_fence = not in_fence
            out.append(line)
            continue
        if not in_fence and _SETEXT_LINE_RE.match(line):
            if out and out[-1].strip():  # add a blank line if the previous line is non-blank
                out.append("")
            out.append(line)
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            if nxt.strip():  # add a blank line if the next line is non-blank
                out.append("")
            continue
        out.append(line)
    return "\n".join(out)


def _make_body_snippet(body: str, start: int, match_len: int) -> str:
    """Build a snippet of ±GREP_SNIPPET_RADIUS chars around the match position in the body.

    After extraction, `_sanitize_snippet` strips markdown structural markers.
    """
    s = max(0, start - GREP_SNIPPET_RADIUS)
    e = min(len(body), start + match_len + GREP_SNIPPET_RADIUS)
    snippet = _sanitize_snippet(body[s:e])
    if s > 0:
        snippet = "…" + snippet
    if e < len(body):
        snippet = snippet + "…"
    return snippet


def grep_score(entry: dict[str, Any]) -> int:
    """Relevance score of an entry. Higher means closer to "the topic of the query itself".

    Computed only from the entry's match_count (not divided by body length; see the constant's note).
    """
    mc = entry["match_count"]
    return (
        mc.get("title", 0) * GREP_TITLE_WEIGHT
        + mc.get("description", 0) * GREP_DESC_WEIGHT
        + mc.get("body", 0)
    )


def grep_subtree(
    root: Node,
    query: str,
    *,
    regex: bool = False,
    limit: int = GREP_DEFAULT_LIMIT,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """Scan the whole subtree from root and collect nodes matching query.

    Each node's 3 fields title / description / body are evaluated independently; a node with
    1+ hits in any of them is returned as 1 entry. Simultaneous hits in several fields of one
    node are expressed as is (the old title-first preference was dropped).

    Matching:
    - regex=False (default): substring + NFKC normalization + case-insensitive
    - regex=True: Python re.compile + re.IGNORECASE. No NFKC normalization (the regex is
      expected to handle it). A compile failure raises ValueError (the caller turns it into 400)

    Returns:
        (matches, total) — matches is the list **sorted by relevance, descending** and then
        sliced to [offset, offset+limit). total is the number of matched nodes (before slicing).

        Ordering is `grep_score` (title×10 + description×5 + body hits) descending, ties by path
        ascending for determinism. **Never return the first N in tree order** — for a reason
        unrelated to content (directory name order), the most relevant document silently drops
        out (measured: for "転倒" the rehabilitation dept with body×16, for "針刺し" the pathology
        dept with body×18, were missing from the first 30).

        Entry shape:
            {
              "path": str,
              "title": str,
              "body_chars": int,                  # body length in chars (0 = no body)
              "child_count": int,
              "match_count": {"title": 0|1, "description": 0|1, "body": int},
              "description"?: str,                # only on a description hit (full text)
              "body_first_match_snippet"?: str,   # only on a body hit (first match ±40 chars)
            }
    """
    if regex:
        try:
            pattern = re.compile(query, re.IGNORECASE)
        except re.error as e:
            raise ValueError(f"bad_regex: {e}") from e
        normalized_query = None
    else:
        pattern = None
        normalized_query = _normalize(query)
        if not normalized_query:
            raise ValueError("query が空")

    def _has_match(raw: str, norm: str) -> bool:
        if regex:
            return bool(raw) and pattern.search(raw) is not None
        return bool(norm) and norm.find(normalized_query) >= 0

    def _body_count_and_first(
        raw: str, norm: str
    ) -> tuple[int, tuple[int, int] | None]:
        """For body: return the total hit count and the first match position (start, length)."""
        if regex:
            if not raw:
                return 0, None
            ms = list(pattern.finditer(raw))
            if not ms:
                return 0, None
            return len(ms), (ms[0].start(), len(ms[0].group(0)))
        if not norm:
            return 0, None
        first = norm.find(normalized_query)
        if first < 0:
            return 0, None
        # NFKC rarely changes length (ligatures etc.), so approximate as a position in the original text
        return norm.count(normalized_query), (first, len(normalized_query))

    matches: list[dict[str, Any]] = []
    total = 0

    def _visit(node: Node) -> None:
        nonlocal total
        desc = node.meta.get("description")
        desc_text = desc if isinstance(desc, str) else None
        n_title, n_desc, n_body = ("", "", "") if regex else _norm_fields(node)
        title_hit = 1 if _has_match(node.title, n_title) else 0
        desc_hit = (
            1 if (desc_text is not None and _has_match(desc_text, n_desc)) else 0
        )
        body_count, body_first = _body_count_and_first(node.body, n_body)

        if title_hit or desc_hit or body_count:
            total += 1
            entry: dict[str, Any] = {
                "path": node.path,
                "title": node.title,
                "body_chars": len(node.body),
                "child_count": len(node.children),
                "match_count": {
                    "title": title_hit,
                    "description": desc_hit,
                    "body": body_count,
                },
            }
            if desc_hit:
                entry["description"] = desc_text
            if body_count and body_first is not None:
                start, mlen = body_first
                entry["body_first_match_snippet"] = _make_body_snippet(
                    node.body, start, mlen
                )
            matches.append(entry)

        for child in node.children.values():
            _visit(child)

    _visit(root)
    # Collect everything, then sort. Truncating first would mean "the first N", and a highly
    # relevant node further back could never be reached (not even via offset).
    matches.sort(key=lambda e: (-grep_score(e), e["path"]))
    return matches[offset : offset + limit], total


# ---------------------------------------------------------------------------
# loose grep (fallback when the literal match has 0 hits)
# ---------------------------------------------------------------------------
#
# exact (substring) treats the whole query string as one term, so natural-language queries like
# "転倒 報告書" or "オシメルチニブの副作用" get 0 hits unless they exist literally.
# The model sends queries in this shape, so only on 0 hits do we split into terms and search again.
#
# No precomputation (index / embeddings). At this scale (measured 913 md / 3.74M chars) sweeping
# every doc with str.count takes a few ms per query, and an index would break walker's guarantee of
# "walk once at startup, no runtime checks afterwards" for little gain.

# BM25 parameters. k1 is the standard value.
# b (strength of document length normalization) is lowered from the standard 0.75 to 0.25 —
# dividing by length pushes index pages of a few hundred chars to the top as noise, as measured
# on the exact side (see the GREP_TITLE_WEIGHT note). b=0 (no normalization) lets huge nodes
# take everything instead, so take the middle.
LOOSE_K1 = 1.2
LOOSE_B = 0.25

# The reference for document length (avgdl) is **not the average over the whole search range, but
# the average of the direct group the node belongs to (the child subtree directly under the search
# range = roughly one ingested corpus)**.
#
# **This is the other half of issue #6 (2026-09-04).** With avgdl over the whole search range,
# **merely adding a corpus unrelated to the query shifts the length penalty of existing nodes**.
# Adding 2,590 shinryo-hoshu (medical fee schedule) nodes (short bodies, avg 1,896 chars) dropped the
# overall average from 2,961 → 2,252 chars, and b made longer nodes penalized more
# (measured: for "バンコマイシンのTDM" the target = the 24,018-char node with both terms
#  lost to a 2,246-char node with only tdm and fell from 1st → 3rd).
#
# Taking it per group measures "long / short" **among documents of the same origin** even in a mixed
# corpus, so no matter how many nodes another corpus adds, the other groups' reference does not move
# (measured: at 4 scales 930–3,895 nodes the target above stays 1st; rank spread over the 21 validation queries is 0).
# When the search range is narrowed to one corpus, all nodes fall in the same group and it matches
# the "average of the search range" as before (= nothing changes for narrowed searches).
#
# But with only a handful of nodes in a group, the average collapses onto the node itself and the
# normalization vanishes. Below this population fall back to the whole-range average (the same
# "do not take statistics on too small a population" judgment as LOOSE_COMMON_MIN_DOCS, so the same value).

# "Common term" = **a term whose IDF falls below this ratio relative to the most informative term in
# the same query**. Such terms are removed from the search entirely (from both the hit condition and
# the score). Terms like "対応" "起" "防止"; keeping them lets them dominate the ranking
# (measured: of the 14.1 score for "針刺し事故が起きたときの対応", 7.7 came from 事故/起/対応, and
#  14-2, with "針刺し" in its title and 針刺×75, lost to a department page that merely had 事故×15 起×14
#  and fell to 8th).
#
# **Moving the criterion from "df / total docs" to "IDF ratio within the query" is the issue #6 fix (2026-09-04).**
# The old rule (`df > total docs × 0.10`) had the number of searched documents as denominator, so
# **merely adding a corpus unrelated to the term made terms flip in and out** — adding 305 FAQ entries
# dropped "事故" (df=124) from 13.3% → 8.9%, it left the stop set, and the 14-2 above sank from 1st → 10th.
# The IDF ratio is relative within the query, so documents on topics absent from the query barely move it
# (measured: over 4 corpus sizes 930–1,388 nodes, the target-rank spread of the 15 design queries went
#  from max 19 → max 2, and of the separate 21 validation queries from max 888 → max 1).
#
# **Do not use min(df) as the criterion** — that was the failure of 2026-08-14, "drop anything over 2.5×
# the rarest term", introduced and withdrawn (end of `docs/cage/16_his_faq.md`). df is linear, so one
# rare term collapses the cut and content terms drop. IDF is logarithmic, so it does not collapse
# (measured: for "MRSAの接触予防策", against "接触予防策" at df=5, mrsa (df=42) drops under the min(df)
#  criterion and the target node disappears with it, but under the IDF ratio it stays at 0.63.
#  "副作用" (df=102) in "オシメルチニブの副作用" likewise stays at 0.54).
#
# Why 0.5: the position that keeps the 2 above while dropping "対応" (0.35) "起" (0.46) "手順" (0.46).
# Raising to 0.7 drops mrsa (0.63) and the MRSA target disappears.
LOOSE_MIN_IDF_RATIO = 0.5
# But in a small subtree df is only a few and the IDF ratio is meaningless (with 4 nodes, df 1 vs 2
# gives a ratio of 0.48 and a term that is not common at all drops). Below this population, skip the
# check (treat all terms as core).
LOOSE_COMMON_MIN_DOCS = 50

# **A lone single kanji is not admitted to core unless it is the most informative term** (2026-09-04).
#
# segment_query cuts at character-class boundaries, so okurigana peels off inflected words and a single
# kanji remains — "起きたときの" → 起, "対する" → 対, "選ぶ" → 選, "抗がん薬" → 抗 + 薬.
# These are not content terms, and we do not want them in the search for the same reason hiragana-only terms are dropped.
#
# **The IDF ratio alone cannot drop them.** The ratio is relative within the query, so it resists the
# df-driven scale dependence, but `idf ≒ log(N/df)` grows for all terms together as N grows, and **the
# ratio itself drifts toward 1**. That is, **the more corpus is added, the more common terms drift toward surviving**
# (measured: the IDF ratio of "起" in "針刺し事故が起きたときの対応" went 0.49 at 930 nodes →
#  0.50 at 3,895 nodes. It crossed the 0.5 threshold into core, and the
#  14_職業感染対策 chapter fell 2nd → 9th. "対応" moved the same way, 0.33 → 0.46, and
#  one more order of magnitude of corpus would do the same).
# Raising the threshold is no escape — at 0.55 or above, misses on the 21 validation queries go
# 3 → 5 (same reason as the mrsa / 副作用 note in the README).
# A df ratio (K× the rarest term) was also measured: a K that drops "起" drops mrsa, and a K that keeps
# mrsa keeps "起" (the window is only K∈(8.4, 9.4)). A replay of the 2026-08-14 failure.
#
# A term's **length** depends only on its character count and not at all on the corpus, so this rule is
# fully invariant to scale. But a single kanji like "癌" "肺" can be a real content term, so
# **it is kept when it is the highest-IDF term in that query** (measured: across the 15 design + 21
# validation queries no single kanji was ever the max IDF, and rankings matched with / without the
# exclusion = a safety-side insurance).
LOOSE_DROP_LONE_KANJI = True

# Among the remaining terms (core), **rarity rank is not treated as importance rank** — any one hit
# makes a candidate
# (measured: making "接触予防策" at df=5 required, for "MRSAの接触予防策" the MRSA node of the
#  per-pathogen manual containing MRSA 104 times dropped entirely just for lacking that literal string).

# Cut at character-class boundaries. Instead of a dictionary, use the property of Japanese that "a
# switch between kanji / hiragana / katakana / alphanumerics is roughly a word boundary". Coarser than
# morphological analysis, but has the decisive advantage for this use that unknown words like drug
# names ("オシメルチニブ") are not split and broken.
_CHAR_CLASS_RANGES = (
    ("hira", 0x3041, 0x309F),
    ("kata", 0x30A0, 0x30FF),
    ("kata", 0x31F0, 0x31FF),  # katakana phonetic extensions
    ("kanji", 0x3005, 0x3006),  # 々 〆
    ("kanji", 0x3400, 0x4DBF),
    ("kanji", 0x4E00, 0x9FFF),
    ("kanji", 0xF900, 0xFAFF),  # CJK compatibility ideographs
)
_PROLONGED = "ー"  # ー (attaches to the preceding kana; not a word boundary on its own)

# Hiragana-only terms are dropped. In this context (hospital manuals, guidelines) content terms are
# almost never written in hiragana; what remains is particles and inflectional endings ("きたときの" "さんが").
# Their IDF is near 0 so they never affected the score, but **mixed into matched_terms / absent_terms
# they muddy the feedback to the model**, so drop them at the entrance.
# Side effect: an all-hiragana query yields 0–1 terms and does not fall back (= exact's 0 hits are
# returned as is). Better than silently returning unrelated results.


def _char_class(ch: str) -> str:
    """Class of one character. 'sep' is a word boundary (symbol / whitespace).

    Letters and digits are not separated. Identifiers like "CQ47" must not be split (splitting
    creates a meaningless term "47" and loses the rarity of the identifier). Symbols remain
    separators, so "ABC-123" becomes 2 terms.
    """
    if ch.isascii() and ch.isalnum():
        return "alnum"
    cp = ord(ch)
    for name, lo, hi in _CHAR_CLASS_RANGES:
        if lo <= cp <= hi:
            return name
    if ch.isalpha():  # Hangul, Cyrillic etc. Lumped into one term
        return "other"
    return "sep"


def segment_query(query: str) -> list[str]:
    """Cut query into search terms (no dictionary; character-class boundaries only).

    After NFKC + lowercase, cut at symbols / whitespace, then at character-class switches.
    Hiragana-only terms are particles / inflectional endings, so they are dropped. Duplicates keep
    only the first occurrence (order preserved).

    Examples:
        "転倒 報告書"           → ["転倒", "報告書"]
        "オシメルチニブの副作用" → ["オシメルチニブ", "副作用"]
        "CQ47 の推奨"           → ["cq47", "推奨"]
    """
    norm = _normalize(query)
    terms: list[str] = []
    cur = ""
    cur_class = ""
    for ch in norm:
        cls = _char_class(ch)
        if ch == _PROLONGED and cur_class in ("kata", "hira"):
            cur += ch  # the prolonged sound mark is part of the preceding kana term
            continue
        if cls == "sep":
            cls = ""
        if cls != cur_class or not cls:
            if cur:
                terms.append(cur)
            cur = ch if cls else ""
            cur_class = cls
        else:
            cur += ch
    if cur:
        terms.append(cur)

    out: list[str] = []
    for t in terms:
        if _char_class(t[0]) == "hira":
            continue
        if t not in out:
            out.append(t)
    return out


def _idf(df_t: int, n_docs: int) -> float:
    """BM25 (smoothed) IDF. Larger for smaller df.

    The "common term" check uses this value too — unless the check uses the same quantity that
    decides the score, the weights of dropped vs. kept terms cannot be explained.
    """
    return math.log(1 + (n_docs - df_t + 0.5) / (df_t + 0.5))


def _is_lone_kanji(term: str) -> bool:
    """Is the term a single kanji (see the `LOOSE_DROP_LONE_KANJI` note).

    A check with no dependence on the corpus, to spot the residue left after segment_query
    peels off okurigana — 起(きた) / 対(する) / 選(ぶ).
    """
    return len(term) == 1 and _char_class(term) == "kanji"


def grep_subtree_loose(
    root: Node,
    terms: list[str],
    *,
    limit: int = GREP_DEFAULT_LIMIT,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int, list[str], list[str]]:
    """BM25F search of the subtree with pre-segmented `terms` (the catch when exact has 0 hits).

    Only 3 differences from exact:

    1. **IDF** — weight by term rarity. The core of not letting the ubiquitous "報告書" dominate
       the ranking for "転倒 報告書". With exact (1 term), IDF is a constant factor common to all
       documents and never moves the ranking, so it only gains meaning with multiple terms.
    2. **TF saturation (k1)** — with exact, hit counts act linearly and a document with 30 hits
       runs over one with 5. Saturate.
    3. **field weights** — title×10 / description×5 / body×1, the same weights as exact, applied
       on the tf side (the most naive form of BM25F). The measured values are used as is.

    The hit condition is **"contains at least 1 of the non-common terms (IDF at least
    `LOOSE_MIN_IDF_RATIO` × the max IDF in that query)"**. A pure OR mixes in hundreds of nodes
    that only have "防止" and match_count loses meaning (measured: without the exclusion, hits for
    "針刺し事故が起きたときの対応" balloon 135 → 495, and "新型コロナウイルス感染症の対応"
    23 → 445); conversely, requiring the rarest term drops a target that lacks the rare term
    entirely (the MRSA measurement in the constant's note).

    Ranking is BM25F multiplied by a **coordination factor** (terms matched / all terms). Plain
    BM25 just sums per-term contributions, so without this a document with only 1 rare term beats
    a document containing all terms.

    Returns:
        (matches, total, absent_terms, ignored_terms)
        Both are feedback for the model:
        - absent_terms: terms appearing nowhere in the subtree (if all are absent, matches is empty)
        - ignored_terms: terms too common, removed from the search
    """
    # group = child subtree directly under the search range (roughly one ingested corpus). The
    # document length reference is taken per group (LOOSE_B note). root itself is "" (= use the whole-range average).
    nodes: list[tuple[Node, str]] = []

    def _collect(node: Node, group: str) -> None:
        nodes.append((node, group))
        for child in node.children.values():
            _collect(child, group or child.path)

    _collect(root, "")

    # The match targets (_norm_fields) are NFKC + lowercased, so bring the terms onto the same footing.
    # Via segment_query they are already normalized and this is idempotent, but **silently getting
    # 0 hits** when given un-normalized terms is the worst outcome, so absorb it at the entrance.
    terms = [t for t in dict.fromkeys(_normalize(t) for t in terms) if t]

    n_docs = len(nodes)
    if not n_docs or not terms:
        return [], 0, list(terms), []

    df: dict[str, int] = {t: 0 for t in terms}
    total_len = 0
    group_len: dict[str, int] = {}
    group_docs: dict[str, int] = {}
    # (node, desc_text, dl, group, {term: (title_tf, desc_tf, body_tf, body_first_pos)})
    hits: list[tuple[Node, str, int, str, dict[str, tuple[int, int, int, int]]]] = []

    for node, group in nodes:
        desc = node.meta.get("description")
        desc_text = desc if isinstance(desc, str) else ""
        n_title, n_desc, n_body = _norm_fields(node)
        dl = len(node.body)
        total_len += dl
        group_len[group] = group_len.get(group, 0) + dl
        group_docs[group] = group_docs.get(group, 0) + 1

        per: dict[str, tuple[int, int, int, int]] = {}
        for t in terms:
            t_tf = n_title.count(t)
            d_tf = n_desc.count(t)
            b_tf = n_body.count(t)
            if t_tf or d_tf or b_tf:
                per[t] = (t_tf, d_tf, b_tf, n_body.find(t) if b_tf else -1)
                df[t] += 1
        if per:
            hits.append((node, desc_text, dl, group, per))

    absent = [t for t in terms if df[t] == 0]
    present = [t for t in terms if df[t] > 0]
    if not present:
        return [], 0, absent, []

    # Terms that count in the search (core) = non-common terms. Both the hit condition and the score
    # are decided by these alone. The check is the **IDF ratio within the query** (reasons and
    # measurements in the `LOOSE_MIN_IDF_RATIO` note). IDF is needed for non-core terms too, so compute it for all present first.
    all_idf = {t: _idf(df[t], n_docs) for t in present}
    top_idf = max(all_idf.values())
    if n_docs >= LOOSE_COMMON_MIN_DOCS:
        floor = top_idf * LOOSE_MIN_IDF_RATIO
        core = [t for t in present if all_idf[t] >= floor]
    else:
        core = list(present)
    # Drop single kanji left over after okurigana peeled off (`LOOSE_DROP_LONE_KANJI` note).
    # The max-IDF term is always kept, so core never becomes empty here.
    if LOOSE_DROP_LONE_KANJI:
        core = [
            t for t in core if not _is_lone_kanji(t) or all_idf[t] >= top_idf
        ]
    # core always has 1+ terms: the max-IDF term has ratio 1.0 and survives the exception above
    # even as a single kanji (in small subtrees all terms survive). The old rescue branch "if all
    # terms are dropped, retry with the single rarest term" became unreachable and was removed.
    ignored = [t for t in present if t not in core]

    avgdl = total_len / n_docs
    # Average document length per group. For a group with a small population the average collapses
    # onto the node itself, so fall back to the whole-range average (`LOOSE_B` note).
    group_avgdl = {
        g: (group_len[g] / group_docs[g]
            if group_docs[g] >= LOOSE_COMMON_MIN_DOCS else avgdl)
        for g in group_docs
    }
    idf = {t: all_idf[t] for t in core}

    scored: list[tuple[float, dict[str, Any]]] = []
    for node, desc_text, dl, group, per in hits:
        if not any(t in per for t in core):
            continue
        ref_dl = group_avgdl.get(group, avgdl)
        score = 0.0
        matched_n = 0
        title_n = desc_n = body_n = 0
        best_term = ""
        best_idf = -1.0
        for t in core:
            got = per.get(t)
            if not got:
                continue
            t_tf, d_tf, b_tf, b_first = got
            matched_n += 1
            title_n += t_tf
            desc_n += d_tf
            body_n += b_tf
            wtf = t_tf * GREP_TITLE_WEIGHT + d_tf * GREP_DESC_WEIGHT + b_tf
            norm = LOOSE_K1 * (
                1 - LOOSE_B + LOOSE_B * (dl / ref_dl if ref_dl else 1.0)
            )
            score += idf[t] * (wtf * (LOOSE_K1 + 1)) / (wtf + norm)
            if b_first >= 0 and idf[t] > best_idf:
                best_idf = idf[t]
                best_term = t

        # coordination factor: discount by how many core terms were matched.
        # Plain BM25 just sums per-term contributions, so a document with many occurrences of 1
        # rare term beats "a document containing all terms" (measured: for "バンコマイシンのTDM",
        # 3 documents with only TDM ranked above those with both). Let the number of matched terms count.
        score *= matched_n / len(core)

        entry: dict[str, Any] = {
            "path": node.path,
            "title": node.title,
            "body_chars": len(node.body),
            "child_count": len(node.children),
            # Same keys as exact, but in loose it is **the sum of occurrences per term**.
            # The only semantic difference from exact: title/description can be 2 or more.
            "match_count": {
                "title": title_n,
                "description": desc_n,
                "body": body_n,
            },
            "matched_terms": [t for t in core if t in per],
        }
        if desc_n:
            entry["description"] = desc_text
        if best_term:
            # The snippet shows the first occurrence of "the rarest term". It conveys why the
            # node was chosen better than the first occurrence of a common term.
            start = _norm_fields(node)[2].find(best_term)
            entry["body_first_match_snippet"] = _make_body_snippet(
                node.body, start, len(best_term)
            )
        scored.append((score, entry))

    scored.sort(key=lambda se: (-se[0], se[1]["path"]))
    matches = [e for _, e in scored]
    return matches[offset : offset + limit], len(matches), absent, ignored


def serialize(node: Node) -> dict[str, Any]:
    """Render the node + its direct children only as a JSON-friendly dict.

    body is never included (the body comes from read).
    meta is attached **only to the looked-up node itself**.
    Each child element is only `{path, title, body_chars, child_count}`.
    To look further down, call list_doc again with that path and dig
    (no nesting, depth is always 1).

    `body_chars` is the body length in chars (0 = no body). Tells "whether calling read on this
    path is worthwhile" and "how large the read payload is" before reading. read itself may be
    called on any node (a body-less node just returns an empty string, not an error).
    """
    children_list: list[dict[str, Any]] = [
        {
            "path": child.path,
            "title": child.title,
            "body_chars": len(child.body),
            "child_count": len(child.children),
        }
        for child in node.children.values()
    ]
    return {
        "path": node.path,
        "title": node.title,
        "meta": node.meta,
        "body_chars": len(node.body),
        "child_count": len(node.children),
        "children": children_list,
    }
