"""doc build (source `data/doc` → artifacts `build/doc`).

cage treats `data/doc` as the **source** and compiles it into artifacts here once at startup.
Every runtime path that reads documents — read_doc / grep_doc / list_doc, the sandbox (read-only
bind at `/ext/doc`), browsers (cage's public route renders HTML per request; render.py) —
**reads only the artifacts**. The source is mounted into neither nucllei nor the sandbox.

Previously only read_doc substituted placeholders and applied the setext workaround per request,
while the sandbox and nucllei's /ext saw the raw, unsubstituted files. This stage exists to
structurally eliminate "different content per path".

Artifacts are only md / yaml in the same relative layout as the source (no HTML is produced):

    leaf X.md          → X.md          (front matter kept byte for byte)
    dir  X/_index.md   → X/_index.md
    dir  X/_index.yaml → X/_index.yaml (a copy with values substituted)

Only 2 transformations: placeholder substitution (1 name = 1 value. Fixed `FIXED_PLACEHOLDERS`:
`{{DOC_BASE}}` → `/ext/doc`, `{{DOC_STATIC_BASE}}` → `/ext/doc-static`; and deployment
`DEPLOY_PLACEHOLDERS`: `{{HIS_FAQ_BASE}}`) and the setext workaround (`isolate_thematic_breaks`).
Doc paths are never guessed from the body and rewritten — the source (each corpus's generator)
writes the canonical form `{{DOC_BASE}}/<category>/…/<name>.md` (directories `…/`) explicitly. nucllei's own variables ({{CURRENT_DATE}} / {{USER_NAME}} /
{{BASE_MODEL_ID}} etc.) are filled by nucllei, so they are left alone. Along the way, 2 lints
(both warnings only; the build is not stopped):
  - duplicate headings (HTML ids escape via `-2` onwards; render.heading_ids) → manifest `duplicate_headings`
  - a path segment contains whitespace or ASCII that breaks URLs / markdown (`PATH_HOSTILE`) → manifest
    `unsafe_paths`. The node address `/ext/doc/<path>.md` becomes a URL as is, and the model writes
    it inside a citation `[…]`. The fix belongs in each corpus's generator (scripts/textfix.safe_segment)
  - links in the body (post-substitution `/ext/doc-static/…` `/ext/doc/…`) have no target, or an old URL
    `/ext/cage/…` / old notation `{{PUBLIC_BASE}}…` remains → manifest `dead_links`
    ({node_path: [link]}). static means a file (under `static_root`), doc means a tree node.
    A doc link that is not canonical (leaf `X.md` / directory `X/`), or a prefix-less old form
    `/<category>/…` (forgot `{{DOC_BASE}}`) that points at a tree node →
    manifest `noncanonical_links` (values are the canonical forms).
    **The watchdog that catches breakage after data is replaced (re-ingesting a revision, `make load KIND=docs`)**.
    A hash of the static listing (names only) is also put in the manifest, so that a rebuild
    (= re-lint) runs even when only static was replaced

Deterministic and idempotent: same source + same deployment values + same build code → same bytes.
The manifest (`<out>.manifest.json`, outside the bind) records the source hash, deployment values and
build code hash; if they match at startup the whole build is skipped. To force a rebuild, delete the
manifest and restart cage. Writes are per file; only files whose content changed are replaced via
temp file → rename. **The output root directory itself is never deleted or recreated** — the sandbox
binds this directory, so replacing it would leave the mount holding the old inode.
Briefly seeing a "mix of old and new" during the build is acceptable (<redacted> 2026-10-05).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context
from pathlib import Path
from typing import Any

import frontmatter

from .walker import (
    DOC_PREFIX,
    STATIC_PREFIX,
    Node,
    isolate_thematic_breaks,
    walk,
)

logger = logging.getLogger("cage.build")

# Version of the artifact format. The hash of the build code (build.py / walker.py / render.py)
# is also in the manifest, so changing the code triggers a rebuild automatically.
BUILD_FORMAT = 5

# Placeholders are 1 name = 1 value. Any {{…}} not listed here is a nucllei variable and is left as text.
# Fixed values (addresses that do not change per deployment; walker constants): the source writes doc
# paths as `{{DOC_BASE}}/<category>/…/<name>.md` (directories `…/`) and figures / originals as `{{DOC_STATIC_BASE}}/…`.
FIXED_PLACEHOLDERS = {"DOC_BASE": DOC_PREFIX, "DOC_STATIC_BASE": STATIC_PREFIX}
# Deployment values (from env; server.deploy_values).
DEPLOY_PLACEHOLDERS = ("HIS_FAQ_BASE",)

MANIFEST_SUFFIX = ".manifest.json"

# git-tracked files kept in the output root (so the directory exists from clone time, owned by
# <redacted>; if compose binds a missing directory it creates it root-owned).
# Not removed by cleanup.
KEEP_FILES = frozenset({".keep"})

CHUNK = 64

# Characters not allowed in a path segment (file / directory name): whitespace (incl. full-width) and
# ASCII that breaks URLs / markdown links. Same set as the generator-side replacement (`safe_segment`,
# `PATH_HOSTILE_ASCII` in scripts/textfix.py) — scripts/test_textfix.py cross-checks them.
PATH_HOSTILE_ASCII = '[]#?%&+"<>\\|^`{}'
PATH_HOSTILE = re.compile(r"[\s" + re.escape(PATH_HOSTILE_ASCII) + "]")


def unsafe_paths(node_paths: list[str]) -> list[str]:
    """Node paths whose segments contain whitespace or `PATH_HOSTILE_ASCII` (deduplicated, input order)."""
    return [p for p in dict.fromkeys(node_paths) if PATH_HOSTILE.search(p)]


# In-doc links in the body (looking at the artifact = post-substitution body). `/ext/doc/…` (nodes) and
# `/ext/doc-static/…` (figures / originals). A leftover old URL `/ext/cage/(static|doc)/…` is always dead
# (neither the public route nor the sandbox has the old URL; fix the hard-coded source).
# The target is a run of URL-safe characters, cut at whitespace, `)`, `>`, `]`, quotes, `|` (tables), `` ` ``
# and Japanese punctuation (some are written inline in prose like `…xlsx。`). `(` `)` pass since names use them
INTERNAL_LINK = re.compile(
    r"/ext/(?:(doc-static|doc)|cage/(static|doc))(?![\w-])"
    r"(/[^\s<>\[\]\"'`|{}、。，．）」』】…]*)?"
)


# Doc link targets may contain `、` `）` `,` in names (`群01_心、心膜、肺`
# `A100_一般病棟入院基本料（１日につき）`), so take a wide run up to whitespace, quotes, `|`, brackets
# (`[]<>{}`), and let target resolution (dead_links) peel trailing punctuation one at a time against the tree.
_DOC_TAIL = re.compile(r"[^\s<>\[\]\"'`|{}]*")
# Old notation `{{PUBLIC_BASE}}…` (moved to `{{DOC_STATIC_BASE}}` on 2026-10-06; a name with no value).
# Unknown `{{…}}` are kept as nucllei variables by rule, so left alone it silently shows up in the body
_LEFTOVER_SOURCE = re.compile(r"\{\{PUBLIC_BASE\}\}[^\s<>\[\]\"'`|(){}、。]*")
_DOC_CUT = set("、。，,．.）)」』】:;*!?…")


def internal_links(text: str, tops: tuple[str, ...] = ()) -> list[tuple[str, str, str]]:
    """In-doc links in the body → [(kind, target, form)].

    kind is `doc` / `static` / `legacy` (old `/ext/cage/…` and old notation `{{PUBLIC_BASE}}…`;
    target is the original string) / `old` (when `tops` is given; prefix-less old form `/<category>/…`;
    target is the original string up to `#`, and `dead_links` checks whether it hits a node).
    The target has `#…` / `?…` dropped and is percent-decoded and NFC-normalized. For static, trailing
    `.,;:*` (end of sentence or emphasis) and unbalanced `)` are dropped. For doc, the wide run after
    `/ext/doc/` is returned as is (form empty); where to cut and the form (`leaf` / `dir` / `index` / `bare`)
    are decided by `doc_link_target` against the tree. Empty targets (`/ext/doc/` alone, or format
    explanations like `/ext/doc/<path>`) are not counted.
    """
    import unicodedata
    from urllib.parse import unquote

    out: list[tuple[str, str, str]] = []
    for m in INTERNAL_LINK.finditer(text):
        kind = m.group(1) or "legacy"
        if kind == "doc":
            tail = _DOC_TAIL.match(text, m.end(1)).group(0)
            rest = unicodedata.normalize("NFC", unquote(re.split(r"[#?]", tail, 1)[0]))
            if rest.strip("/") and "…" not in rest:  # containing `…` means a format explanation
                out.append(("doc", rest, ""))
            continue
        rest = re.split(r"[#?]", m.group(3) or "", 1)[0].rstrip(".,;:*")
        # Some file names contain `(` `)`, so `)` passes; only an unbalanced trailing `)`
        # (the markdown link close) is dropped
        while rest.endswith(")") and rest.count(")") > rest.count("("):
            rest = rest[:-1].rstrip(".,;:*")
        if kind == "legacy":
            out.append((kind, f"/ext/cage/{m.group(2)}{rest}", ""))
            continue
        rest = unicodedata.normalize("NFC", unquote(rest))
        rel = rest.strip("/")
        if rel:
            out.append(("static", rel, ""))
    out.extend(("legacy", m.group(0), "") for m in _LEFTOVER_SOURCE.finditer(text))
    out.extend(("old", ref, "") for ref in old_form_refs(text, tops))
    return out


# Lint for prefix-less old-form doc paths (`/<category>/…`). Until 2026-10-06 the build rewrote them
# to canonical here (canonicalize_doc_refs), but guessing from the body was dropped in favor of the
# source writing `{{DOC_BASE}}/…` explicitly. Only the same extraction rule remains, as a watchdog for omissions:
# a run starting with `/<top-level category>` right after a delimiter (line start, whitespace, `` ` ``, `"`, `(`, `「` etc.).
# Mid-URL (`https://host/院内…`, `/ext/doc/院内…`) does not match since the preceding char is alphanumeric or `/`.
# Whether it hits a node (= an omission, vs. a format explanation or something else) is checked by dead_links against the tree.
_OLD_REF_STOP = set(" \t\n`\"'|]、。，,」』）>")
_TRIM_OK = set(").:;*!?")


def old_form_refs(text: str, tops: tuple[str, ...]) -> list[str]:
    """Runs in text that look like prefix-less doc paths (up to `#…`)."""
    if not tops or "/" not in text:
        return []
    starts = re.compile(
        r"(?<![\w/.\-%:])/(?:" + "|".join(re.escape(t) for t in tops) + r")(?=[/\s`\"'|)\]、。#]|$)"
    )
    out: list[str] = []
    for m in starts.finditer(text):
        e = m.start()
        quote = text[m.start() - 1] if m.start() else ""
        if quote in ("`", '"'):
            # Inside `…` / "…" everything up to the close is one unit (some node names contain `、` `,`)
            nl = text.find("\n", e)
            close = text.find(quote, e, nl if nl != -1 else len(text))
            e = close if close != -1 else e
        if e == m.start():
            while e < len(text) and text[e] not in _OLD_REF_STOP:
                e += 1
        out.append(text[m.start():e].partition("#")[0])
    return out


def old_form_target(path: str, node_kinds: dict[str, bool]) -> str | None:
    """Old-form run → Node.path of the node it hits (None if none). Only trailing sentence punctuation
    (`)` `.` `:` etc.) is trimmed, longest match (cutting mid-name or before a `/` would point at a different node)."""
    cand = path
    while cand:
        rest = path[len(cand):]
        if not rest or rest[0] in _TRIM_OK:
            key = cand.rstrip("/")
            if key.endswith(".md"):
                key = key[: -len(".md")]
            if key in node_kinds:
                return key
        cand = cand[:-1]
    return None


def _doc_form(rest: str) -> tuple[str, str]:
    """`/A/B.md` etc. (after `/ext/doc`) → (Node.path without the leading `/`, form)."""
    rel = rest.strip("/")
    if rel == "_index.md" or rel.endswith("/_index.md"):
        return rel[: -len("_index.md")].rstrip("/"), "index"
    if rel.endswith(".md"):
        return rel[: -len(".md")], "leaf"
    return rel, ("dir" if rest.endswith("/") else "bare")


def doc_link_target(rest: str, node_kinds: dict[str, bool]) -> tuple[str, str, bool | None]:
    """Doc link target (the wide rest from `internal_links`) → (rel, form, is directory).

    Starting from the whole rest, try the forms cut before trailing punctuation (`_DOC_CUT`) longest
    first and take the first one present in the tree. If none, rel is up to the first punctuation and is_dir=None.
    """
    cuts = [len(rest)] + [i for i in range(len(rest) - 1, 0, -1) if rest[i] in _DOC_CUT]
    for i in cuts:
        rel, form = _doc_form(rest[:i])
        if rel and ("/" + rel) in node_kinds:
            return rel, form, node_kinds["/" + rel]
    # When absent, report up to the Japanese delimiter (do not cut at the `.` of `.md`)
    first = min([i for i in range(1, len(rest)) if rest[i] in "、。，,）」』】"] or [len(rest)])
    while rest[:first].endswith((")", ".", ":", ";", "*")) and not rest[:first].endswith(".md"):
        first -= 1
    rel, form = _doc_form(rest[:first])
    return rel, form, None


def dead_links(
    links: dict[str, list[tuple[str, str, str]]],
    node_kinds: dict[str, bool],
    static_root: Path | None,
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """({node_path: [links with no target]}, {node_path: [non-canonical doc links]}).

    `node_kinds` is Node.path → is directory. If static_root is None, static is not checked.
    Non-canonical doc links (leaf without `.md`, directory without trailing `/`, `_index.md`) do open
    since the public route redirects to canonical, but the citation number is counted as a different address.
    Prefix-less old forms (`old`) are counted as non-canonical only when they hit a node (non-hits
    are format explanations or something else, so they are ignored).
    """
    dead: dict[str, list[str]] = {}
    noncanon: dict[str, list[str]] = {}
    exists: dict[str, bool] = {}
    for node, items in sorted(links.items()):
        bad: list[str] = []
        odd: list[str] = []
        for kind, rel, _form in items:
            if kind == "legacy":
                bad.append(rel)
                continue
            if kind == "old":
                key = old_form_target(rel, node_kinds)
                if key is not None and key != "/":
                    odd.append(f"{DOC_PREFIX}{key}" + ("/" if node_kinds[key] else ".md"))
                continue
            if kind == "doc":
                target, form, is_dir = doc_link_target(rel, node_kinds)
                if not target:  # `/ext/doc/_index.md` = root
                    continue
                if is_dir is None:
                    bad.append(f"{DOC_PREFIX}/{target}")
                elif form != ("dir" if is_dir else "leaf"):
                    odd.append(f"{DOC_PREFIX}/{target}" + ("/" if is_dir else ".md"))
                continue
            if static_root is None:
                continue
            if rel not in exists:
                exists[rel] = ".." not in rel.split("/") and (static_root / rel).is_file()
            if not exists[rel]:
                bad.append(f"{STATIC_PREFIX}/{rel}")
        if bad:
            dead[node] = list(dict.fromkeys(bad))
        if odd:
            noncanon[node] = list(dict.fromkeys(odd))
    return dead, noncanon


def _node_kinds(tree: Node) -> dict[str, bool]:
    """Node.path → is directory (all nodes, including directories without an index)."""
    out: dict[str, bool] = {}

    def _visit(n: Node) -> None:
        out[n.path] = n.is_dir
        for c in n.children.values():
            _visit(c)

    _visit(tree)
    return out


def static_listing_hash(static_root: Path | None) -> str:
    """Hash of the file name listing under static (contents not read). '' if absent.

    Reading contents would mean reading the whole data bundle (about 1GB) at every startup.
    Dead link detection only needs "does a file with that name exist", so names suffice.
    """
    if static_root is None or not static_root.is_dir():
        return ""
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(static_root):
        dirnames.sort()
        rel = Path(dirpath).relative_to(static_root).as_posix()
        for name in sorted(filenames):
            h.update(f"{rel}/{name}".encode("utf-8") + b"\0")
    return h.hexdigest()


# ---------------------------------------------------------------------------
# md transformation
# ---------------------------------------------------------------------------


def substitute(text: str, values: dict[str, str]) -> str:
    """Substitute only the fixed (`FIXED_PLACEHOLDERS`) and deployment (`DEPLOY_PLACEHOLDERS`)
    placeholders (other {{…}} are kept)."""
    for name, value in FIXED_PLACEHOLDERS.items():
        text = text.replace("{{" + name + "}}", value)
    for name in DEPLOY_PLACEHOLDERS:
        text = text.replace("{{" + name + "}}", values.get(name, ""))
    return text


def compile_md(
    raw: str,
    values: dict[str, str],
) -> tuple[str, str]:
    """Full source md → (full artifact md, body).

    Front matter is kept byte for byte (so grepping front matter in the sandbox keeps working);
    the setext workaround is applied to the body only. Substitution applies to the whole text including front matter.
    The output is shaped so that re-reading with walker (python-frontmatter) yields a body equal to
    the returned body (python-frontmatter strips the whole text and the body separately).
    """
    text = substitute(raw, values).strip()
    handler = frontmatter.detect_format(text, frontmatter.handlers)
    prefix, content = "", text
    if handler is not None:
        try:
            _, _fm, content = handler.FM_BOUNDARY.split(text, 2)
            prefix = text[: len(text) - len(content)]
        except ValueError:
            prefix, content = "", text
    body = isolate_thematic_breaks(content.strip())
    out = (prefix + body).rstrip() + "\n" if (prefix or body) else ""
    return out, body


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------


def _write_if_changed(path: Path, data: str) -> bool:
    """Write via temp file → rename only when the content changed. True if written."""
    b = data.encode("utf-8")
    try:
        if path.read_bytes() == b:
            return False
    except FileNotFoundError:
        pass
    _mkdirs(path.parent)
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
    tmp.write_bytes(b)
    os.chmod(tmp, 0o644)  # readable by the sandbox (different uid) regardless of umask
    os.replace(tmp, path)
    return True


def _mkdirs(d: Path) -> None:
    if d.is_dir():
        return
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o755)


_WORKER_CTX: tuple[Path, Path, dict[str, str], Any, tuple[str, ...]] | None = None


def _worker_init(
    src_root: str, out_root: str, values: dict[str, str],
    tops: tuple[str, ...] = (),
) -> None:
    global _WORKER_CTX
    from .render import make_md

    _WORKER_CTX = (Path(src_root), Path(out_root), values, make_md(), tops)


def _run_jobs(
    jobs: list[tuple[str, str]],
) -> list[tuple[str, list[str], int, list[tuple[str, str, str]]]]:
    """Worker body. jobs are (node_path, source relative path).

    Returns a list of (node_path, duplicated heading texts, files written, in-doc links).
    """
    from .render import heading_ids

    assert _WORKER_CTX is not None
    src_root, out_root, values, md, tops = _WORKER_CTX
    results = []
    for node_path, rel in jobs:
        raw = (src_root / rel).read_text(encoding="utf-8")
        dups: list[str] = []
        links: list[tuple[str, str, str]] = []
        if rel.endswith(".md"):
            out_text, body = compile_md(raw, values)
            # The heading lint counts by the same rule as HTML id numbering (same function)
            _ids, dups = heading_ids(md.parse(body))
            links = internal_links(out_text, tops)
        else:  # yaml: no body. A copy with values substituted
            out_text = substitute(raw, values)
        written = int(_write_if_changed(out_root / rel, out_text))
        results.append((node_path, dups, written, links))
    return results


def plan(tree: Node, src_root: Path) -> list[tuple[str, str]]:
    """Source tree → list of (node_path, source relative path). Dirs without an index are not emitted.

    walker's key is "stem for files, name for directories", so each segment of node.path is the
    file name (stem) as is — the `05_…` number prefix and names containing `.` (`RECIST1.1`) included.
    Artifacts share the same relative layout, so in the sandbox a node is readable at its address
    (walker.node_addr: leaf `/ext/doc/<path>.md`, dir `/ext/doc/<path>/` via `_index.md`).
    """
    jobs: list[tuple[str, str]] = []

    def _visit(node: Node) -> None:
        segs = [] if node.path == "/" else node.path.strip("/").split("/")
        d = src_root.joinpath(*segs)
        rel = "/".join(segs)
        if d.is_dir():
            for name in ("_index.md", "_index.yaml"):
                if (d / name).is_file():
                    jobs.append((node.path, f"{rel}/{name}" if rel else name))
                    break
            for c in node.children.values():
                _visit(c)
        else:
            ext = ".md" if (d.parent / f"{d.name}.md").is_file() else ".yaml"
            jobs.append((node.path, rel + ext))

    _visit(tree)
    return jobs


def source_hash(src_root: Path) -> str:
    """Hash of all source files (relative path + content). Traversal is in name order, deterministic."""
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(src_root):
        dirnames.sort()
        for name in sorted(filenames):
            p = Path(dirpath) / name
            h.update(p.relative_to(src_root).as_posix().encode("utf-8") + b"\0")
            h.update(p.read_bytes())
            h.update(b"\0")
    return h.hexdigest()


def code_hash() -> str:
    """Hash of the code that affects the build output (and lint) plus the markdown-it version."""
    import markdown_it

    h = hashlib.sha256(f"{BUILD_FORMAT}:{markdown_it.__version__}".encode())
    here = Path(__file__).resolve().parent
    for name in ("build.py", "walker.py", "render.py"):
        h.update((here / name).read_bytes())
    return h.hexdigest()


def manifest_path(out_root: Path) -> Path:
    return out_root.with_name(out_root.name + MANIFEST_SUFFIX)


def _cleanup(out_root: Path, keep: set[str]) -> int:
    """Remove files not in the artifacts (vanished nodes, half-written temp files) and empty directories.
    The output root itself and KEEP_FILES are kept."""
    removed = 0
    for dirpath, _dirnames, filenames in os.walk(out_root, topdown=False):
        d = Path(dirpath)
        for name in filenames:
            rel = (d / name).relative_to(out_root).as_posix()
            if rel not in keep and rel not in KEEP_FILES:
                (d / name).unlink()
                removed += 1
        if d != out_root and not any(d.iterdir()):
            d.rmdir()
    return removed


def build(
    src_root: Path,
    out_root: Path,
    values: dict[str, str],
    *,
    workers: int | None = None,
    force: bool = False,
    static_root: Path | None = None,
) -> dict[str, Any]:
    """Compile the source into artifacts and return the manifest. Skipped if a matching manifest exists.

    If `out_root` already exists only its contents are replaced (the directory itself is not recreated).
    Source validation errors (WalkerError) propagate = cage does not start.
    Passing `static_root` (root of served files) also lints broken links to static.
    """
    t0 = time.perf_counter()
    values = {k: values.get(k, "") for k in DEPLOY_PLACEHOLDERS}
    src_hash = source_hash(src_root)
    st_hash = static_listing_hash(static_root)
    c_hash = code_hash()
    mpath = manifest_path(out_root)
    if not force and mpath.is_file() and out_root.is_dir():
        try:
            old = json.loads(mpath.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            old = None
        if (
            old
            and old.get("source_hash") == src_hash
            and old.get("code_hash") == c_hash
            and old.get("values") == values
            and old.get("static_hash") == st_hash
        ):
            logger.info(
                "build: up to date (manifest %s, built_at=%s) — skip", mpath, old.get("built_at")
            )
            _log_dups(old.get("duplicate_headings", {}))
            _log_unsafe(old.get("unsafe_paths", []))
            _log_dead(old.get("dead_links", {}))
            _log_noncanon(old.get("noncanonical_links", {}))
            old["skipped"] = True
            return old

    tree = walk(src_root)  # validation (on failure, do not start)
    jobs = plan(tree, src_root)
    kinds = _node_kinds(tree)
    tops = tuple(c for c in tree.children)

    _mkdirs(out_root)
    # If we die midway, the next run must rebuild (so a stale manifest is never treated as a match)
    mpath.unlink(missing_ok=True)

    n_workers = workers or min(os.cpu_count() or 1, 32)
    chunks = [jobs[i : i + CHUNK] for i in range(0, len(jobs), CHUNK)]
    results: list[tuple[str, list[str], int, list[tuple[str, str, str]]]] = []
    if n_workers <= 1:
        _worker_init(str(src_root), str(out_root), values, tops)
        for ch in chunks:
            results += _run_jobs(ch)
    else:
        # spawn: do not fork from uvicorn's lifespan (event loop and threads present)
        with ProcessPoolExecutor(
            max_workers=n_workers,
            mp_context=get_context("spawn"),
            initializer=_worker_init,
            initargs=(str(src_root), str(out_root), values, tops),
        ) as ex:
            for r in ex.map(_run_jobs, chunks):
                results += r

    keep = {rel for _p, rel in jobs}
    removed = _cleanup(out_root, keep)
    dups = {p: d for p, d, _w, _l in sorted(results) if d}
    unsafe = unsafe_paths(sorted(p for p, _rel in jobs))
    written = sum(w for _p, _d, w, _l in results)
    dead, noncanon = dead_links(
        {p: links for p, _d, _w, links in results if links},
        kinds,
        static_root,
    )
    elapsed = time.perf_counter() - t0
    manifest = {
        "format": BUILD_FORMAT,
        "source": str(src_root),
        "source_hash": src_hash,
        "code_hash": c_hash,
        "values": values,
        "static_root": str(static_root) if static_root is not None else None,
        "static_hash": st_hash,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "elapsed_s": round(elapsed, 2),
        "files": len(keep),
        "written": written,
        "removed": removed,
        "duplicate_headings": dups,
        "unsafe_paths": unsafe,
        "dead_links": dead,
        "noncanonical_links": noncanon,
    }
    tmp = mpath.with_name(mpath.name + ".tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, mpath)
    logger.info(
        "build: %s → %s (files=%d written=%d removed=%d workers=%d) in %.1fs",
        src_root, out_root, len(keep), written, removed, n_workers, elapsed,
    )
    _log_dups(dups)
    _log_unsafe(unsafe)
    _log_dead(dead)
    _log_noncanon(noncanon)
    manifest["skipped"] = False
    return manifest


def _log_dups(dups: dict[str, list[str]]) -> None:
    """Warn about duplicate headings within one node (build continues; ids escape via `-2` onwards)."""
    if not dups:
        return
    sample = "; ".join(f"{p}: {', '.join(d[:3])}" for p, d in list(dups.items())[:5])
    logger.warning(
        "build: %d nodes have duplicate headings (HTML ids get -2, -3…; full list in "
        "manifest duplicate_headings). e.g. %s",
        len(dups), sample,
    )


def _log_unsafe(paths: list[str]) -> None:
    """Warn about paths that are awkward in URLs (build continues)."""
    if not paths:
        return
    logger.warning(
        "build: %d node paths contain whitespace or URL-hostile characters (%s) — fix the "
        "corpus generator (scripts/textfix.safe_segment); full list in manifest unsafe_paths. e.g. %s",
        len(paths), PATH_HOSTILE_ASCII, "; ".join(paths[:5]),
    )


def _log_noncanon(links: dict[str, list[str]]) -> None:
    """Warn about non-canonical doc links (build continues; the public route redirects, so they open)."""
    if not links:
        return
    n = sum(len(v) for v in links.values())
    sample = "; ".join(f"{p}: {v[0]}" for p, v in list(links.items())[:5])
    logger.warning(
        "build: %d doc links in %d nodes are not in the canonical form (leaf X.md / dir X/; "
        "full list in manifest noncanonical_links, values are the canonical forms). e.g. %s",
        n, len(links), sample,
    )


def _log_dead(dead: dict[str, list[str]]) -> None:
    """Warn about in-doc links with no target (build continues)."""
    if not dead:
        return
    n = sum(len(v) for v in dead.values())
    sample = "; ".join(f"{p}: {v[0]}" for p, v in list(dead.items())[:5])
    logger.warning(
        "build: %d dead links in %d nodes (static file or doc node missing — data replaced "
        "without regenerating the corpus? fix the corpus generator and regenerate; full list "
        "in manifest dead_links). e.g. %s",
        n, len(dead), sample,
    )


def main(argv: list[str] | None = None) -> int:
    """For debugging: `uv run python -m cage.build --out DIR [--force]` — run only the build with
    the same settings as the server.

    Not used in operation (the same thing runs at cage startup). Artifacts are written to DIR
    (manifest at `DIR.manifest.json`). For trying the build and lint (dead_links etc.) without
    touching the `build/doc` a running cage is serving. **`--out` is required** — it used to default
    to `build/doc`, and twice someone meaning to run `--help` overwrote the artifacts being served (2026-10-06).
    """
    import argparse

    ap = argparse.ArgumentParser(prog="python -m cage.build", description="Build the doc source into DIR and lint it (does not write to the served build/doc)")
    ap.add_argument("--out", required=True, type=Path, metavar="DIR", help="output directory (manifest at DIR.manifest.json)")
    ap.add_argument("--force", action="store_true", help="rebuild even if the hashes match")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)
    out_arg = args.out.resolve()
    from dotenv import load_dotenv

    from .__main__ import REPO_ROOT, ROOT

    load_dotenv(ROOT / ".env", override=False)
    load_dotenv(REPO_ROOT / ".env", override=False)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    from . import server  # import after reading env (DATA_ROOT etc. are decided from env)

    m = build(
        server.DATA_ROOT, out_arg, server.deploy_values(),
        force=args.force, static_root=server.STATIC_ROOT,
    )
    lists = ("duplicate_headings", "unsafe_paths", "dead_links", "noncanonical_links")
    summary = {k: v for k, v in m.items() if k not in lists}
    summary.update({k: len(m.get(k) or ()) for k in lists})
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
