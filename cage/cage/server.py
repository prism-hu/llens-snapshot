"""cage API server (doc domain).

cage is the HTTP tool gateway called from LLENS. Each domain lives under a
`/<domain>/<verb>` path. This file is the `doc` domain:

POST /doc/list → node metadata and child tree (no body)
POST /doc/read → node body Markdown (plain text; the node address is in the X-Doc-Path header)
POST /doc/grep → subtree full-text search (title / description / body)
GET/HEAD /ext/doc/<addr>         → HTML for browsers (relayed by nucllei's /ext/doc proxy; render.py)
GET/HEAD /ext/doc-static/<…>     → files under data/static (Range / 304; relayed by the /ext/doc-static proxy)

The `mon` domain (Prometheus monitoring data) includes the router from mon.py.

A doc address is one string: **tool path = sandbox path = page URL = citation** (walker.node_addr):
- "/ext/doc/" → root
- "/ext/doc/診療ガイドライン/肺癌2025_非小細胞癌/" → section (directories end with `/`)
- "/ext/doc/診療ガイドライン/肺癌2025_非小細胞癌/CQ47.md" → CQ leaf (the real file in the sandbox)
**Lenient input, one output form**: the request `path` is accepted as the same node regardless of
the `/ext/doc` prefix, `.md`, trailing `/`, or a directory's `_index.md` (walker.addr_to_path), and
the response `path` (each list / grep entry, not_found's valid_path, read's X-Doc-Path) is
always the canonical form. Entries carry no separate `url` (the path is the URL).

At startup `data/doc/` (source) is compiled into `build/doc/` (artifacts) (build.py; skipped
when the manifest matches), and **the artifacts** are walked to build the whole tree in memory.
What read / grep / list return is the artifacts = the same files the sandbox and nucllei's /ext see.
No change detection or reload mechanism (restart the server when data changes).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import build as doc_build
from .mon import router as mon_router
from .render import make_md, node_href, not_found_page, render_node
from .walker import (
    DOC_PREFIX,
    GREP_DEFAULT_LIMIT,
    GREP_MAX_LIMIT,
    ROOT_PATH_ALIASES,
    STATIC_PREFIX,
    Node,
    addr_to_path,
    grep_subtree,
    grep_subtree_loose,
    navigate,
    node_addr,
    segment_query,
    serialize,
    split_path,
    walk,
)

# Root of the document corpus. Defaults to data/doc in the repo; overridable via CAGE_DATA_ROOT.
# The image is code-only. data/ (documents + static assets + ingestion masters) contains in-hospital
# documents and original PDFs, so it goes into neither git nor the image; it is shipped as a Zot
# artifact (tar) and bind-mounted (`make data-publish` / `data-restore`, no image rebuild). See docs/airgap.md.
DATA_ROOT = Path(os.environ.get(
    "CAGE_DATA_ROOT", str(Path(__file__).resolve().parent.parent / "data" / "doc")))

# doc build artifacts (build.py). read / grep / list and the public-route HTML use the tree walked
# from this; the sandbox reads this directory via a read-only bind at /ext/doc (docker-compose.yml).
# The source data/doc is mounted nowhere. Being a build artifact, it lives in neither Sync nor git.
# Only the directory itself exists from clone time, owned by <redacted>, via git's `.keep`
# (if compose binds a missing directory it creates it root-owned and cage cannot write).
# Overriding CAGE_DATA_ROOT does not change the output dir (tests override it in conftest.py).
DOC_OUT_ROOT = Path(__file__).resolve().parent.parent / "build" / "doc"

# The public routes (what browsers / the nucllei server fetch with GET / HEAD) are `/ext/doc/…` and
# `/ext/doc-static/…` (walker.DOC_PREFIX / STATIC_PREFIX). nucllei relays `/ext/doc/**` `/ext/doc-static/**`
# as is, with session auth, via EXT_MOUNTS `doc=<cage>/ext/doc,doc-static=<cage>/ext/doc-static`, so the
# cage-side URL, the browser URL and the sandbox path are the same string. Making it configurable per
# deployment would break "path = URL = sandbox path", so it is not an env var
# (CAGE_PUBLIC_PREFIX / CAGE_PUBLIC_BASE removed 2026-10-06).
# No auth on this route (the nucllei proxy owns it; cage's reachability stays UFW-limited to the
# Docker bridge). Neither path nor method overlaps with the tool RPC (POST /doc/* etc.).
for _gone in ("CAGE_PUBLIC_BASE", "CAGE_PUBLIC_PREFIX"):
    if os.environ.get(_gone):
        logging.getLogger("cage.doc").warning(
            "%s is set but no longer used (doc is served at %s/, figures at %s/; "
            "remove it from .env)", _gone, DOC_PREFIX, STATIC_PREFIX,
        )

# Headers for the public-route HTML / md. nucllei overrides the CSP, but attach the same one
# for when cage is hit directly.
PUBLIC_CSP = (
    "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'self'"
)

# Static file serving (cropped figure images, split original PDFs etc.). Defaults to data/static in
# the repo; overridable via CAGE_STATIC_ROOT. Like doc, read-only bind-mounted as part of the data bundle.
# Static files only use GET ("all endpoints POST" is the tool RPC convention; browser-facing serving is exempt).
STATIC_ROOT = Path(os.environ.get(
    "CAGE_STATIC_ROOT", str(Path(__file__).resolve().parent.parent / "data" / "static")))

# Root of the hospital information system FAQ app (his-info). Replaces {{HIS_FAQ_BASE}} in bodies.
# e.g. staging(<redacted-net>)    https://<redacted-intranet-host>/his-info
#      production(<redacted-net>) http://<redacted-ip>/his-info
#
# **Deliberately claims no more than "the root of the FAQ app".** Holding just the origin under a
# name like HIS_BASE would be an unverified promise that "the whole HIS intranet is here".
# All this substitution guarantees is that faq.aspx / faq_second.aspx / log_write.aspx live under
# the same base, which matches exactly what was verified when the FAQ was ingested.
HIS_FAQ_BASE = os.environ.get("CAGE_HIS_BASE", "").rstrip("/")


def deploy_values() -> dict[str, str]:
    """Deployment values filled in at build time (corresponds to build.DEPLOY_PLACEHOLDERS)."""
    return {"HIS_FAQ_BASE": HIS_FAQ_BASE}


logger = logging.getLogger("cage.doc")

# Test for splitting loose absent_terms into 2 kinds. Terms containing katakana / prolonged
# sound mark / alphanumerics are drug names, product names, proper nouns = if absent, truly not
# in the corpus. Kanji-only terms may just be a mis-segmented compound (see note generation in doc_grep).
_HAS_KANA_OR_ASCII = re.compile(r"[ァ-ヶーｦ-ﾟa-zA-Z0-9]")


class UTF8JSONResponse(JSONResponse):
    """JSON without ASCII escaping. Japanese stays as Japanese on the wire."""

    def render(self, content: Any) -> bytes:
        return json.dumps(
            content,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Source → artifacts. Source validation errors are raised here and the server does not start.
    manifest = doc_build.build(DATA_ROOT, DOC_OUT_ROOT, deploy_values(), static_root=STATIC_ROOT)
    app.state.doc_manifest = manifest
    tree = walk(DOC_OUT_ROOT)
    app.state.tree = tree
    section_paths = [c.path for c in tree.children.values()]
    logger.info("walker: built tree, sections=%s", section_paths)
    if not HIS_FAQ_BASE:
        logger.warning(
            "CAGE_HIS_BASE is not set: {{HIS_FAQ_BASE}} in doc bodies is replaced "
            "with an empty string, so HIS FAQ original links will not open"
        )
    yield


app = FastAPI(
    title="cage",
    version="0.1",
    lifespan=lifespan,
    default_response_class=UTF8JSONResponse,
)


# mon domain (aggregates Prometheus monitoring data via named queries; top of cage/mon.py).
app.include_router(mon_router, default_response_class=UTF8JSONResponse)


# Public-route static: `/ext/doc-static/…` → data/static. StaticFiles provides GET / HEAD,
# Range (206), ETag / Last-Modified with 304, path traversal rejection, and Content-Type
# (mimetypes; svg is image/svg+xml).
if STATIC_ROOT.is_dir():
    app.mount(STATIC_PREFIX, StaticFiles(directory=STATIC_ROOT), name="static")


@app.middleware("http")
async def public_headers(request: Request, call_next):
    """Attach nosniff to public-route responses (the CSP for HTML / md is attached per response)."""
    response = await call_next(request)
    if request.url.path.startswith("/ext/"):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
    return response


@app.middleware("http")
async def log_request(request: Request, call_next):
    """Log each request's method, URL, status and elapsed time in one line. Details are logged inside each endpoint."""
    start = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - start) * 1000
    logger.info(
        "%s %s → %d (%.1fms)",
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
    )
    return response


class LookupRequest(BaseModel):
    path: str = Field(default="/")


class ReadRequest(BaseModel):
    path: str = Field(default="/")


class GrepRequest(BaseModel):
    query: str
    path: str = Field(default="/")
    regex: bool = Field(default=False)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=GREP_DEFAULT_LIMIT, ge=1, le=GREP_MAX_LIMIT)


def loose_note(terms: list[str], absent: list[str], ignored: list[str]) -> str:
    """Natural-language feedback for the model, attached to loose responses.

    Kept as a pure function because the wording is the spec itself (it drives the model's
    behavior), so it can be tested without depending on the corpus.

    Terms that missed are the most effective feedback, so they go first. But **a miss means
    one of 2 things**, so they are worded apart (measured: 2 of 10 realistic queries produced
    absent terms, but both were kanji compounds "副作用管理" / "副作用対策" while "副作用"
    itself is abundant = false alarms from coarse segmentation). Terms with kana / alphanumerics
    are drug or product names, so if absent they are truly not in the corpus.
    """
    hard = [t for t in absent if _HAS_KANA_OR_ASCII.search(t)]
    soft = [t for t in absent if t not in hard]
    # Shape follows the nucllei convention (README_API §5.1): statement of fact (may be relayed
    # to the user) first, instructions aimed only at the model after "次の手: " (next step).
    # Term lists are joined with "、" (a Python list literal copied into an answer is unreadable).
    # Internal terms (BM25 / fallback / subtree) are not written, since the model may copy them into answers.
    # Even for hard misses, do not let the model conclude "no match" at once — judgment follows the
    # prompt (doc section of llens-thinking); here only the fact and next step (alt spellings, abbreviations) are given.
    fact = ""
    nxt = ""
    if hard:
        fact += (
            f"次の語はこの範囲の収録文書に 1 件も出てこない: {'、'.join(hard)}。"
            "薬剤名・製品名などの固有名詞なので、収録が無い可能性がある。"
        )
        nxt += f"{'、'.join(hard)} は別表記・略語でも探してから判断する。"
    if soft:
        fact += f"次の語はこの並びのままでは出てこない: {'、'.join(soft)}。"
        nxt += (
            f"{'、'.join(soft)} は語の切り方が粗いだけのことがあるので、短く分けて grep_doc を"
            "掛け直すと見つかることがある。"
        )
    used = [t for t in terms if t not in absent and t not in ignored]
    return (
        fact + "query どおりの字面は 0 件だったので、語に分けて探した。"
        f"効かせた語: {'、'.join(used) if used else '(なし)'}。"
        + (f"ありふれすぎるので外した語: {'、'.join(ignored)}。" if ignored else "")
        + "次の手: " + nxt
        + "当たりは語ごとで、query 全体が本文にあるとは限らない (各 entry の matched_terms が"
        "そのノードで当たった語、match_count は語ごとの出現回数の合計)。"
        "字面どおりに探したいときは語を減らして grep_doc を掛け直す。"
        "引用する前に read_doc で本文を確かめる。"
    )


def _with_addr(entry: dict[str, Any], node: Node) -> dict[str, Any]:
    """Replace the entry's `path` (internal Node.path) with the canonical address (key order unchanged)."""
    return {k: (node_addr(node) if k == "path" else v) for k, v in entry.items()}


def _resolve_req(path: str, tree: Node) -> tuple[str, Node | None] | UTF8JSONResponse:
    """Requested path → (internal Node.path, node or None). A 400 response if the form is unparseable."""
    if path == STATIC_PREFIX or path.startswith(STATIC_PREFIX + "/"):
        # A figure / original file. Not a doc node (the tool guides with a dedicated message)
        return UTF8JSONResponse(
            status_code=400,
            content={"error": "static_path", "message": "図・原本のファイルは doc のノードではない"},
        )
    try:
        internal = addr_to_path(path)
        return internal, navigate(tree, internal)
    except ValueError as e:
        return UTF8JSONResponse(
            status_code=400,
            content={"error": "bad_path", "message": str(e)},
        )


def _not_found(req_path: str, tree: Node) -> UTF8JSONResponse:
    """404 response. Returns the first failing segment, the valid path up to that point (canonical
    address), and the sibling nodes available there (names in the address-tail form) so the AI can correct the path.
    `req_path` is in internal Node.path notation (already through addr_to_path).
    """
    try:
        segments = split_path(req_path)
    except ValueError as e:
        return UTF8JSONResponse(
            status_code=400,
            content={"error": "bad_path", "message": str(e)},
        )

    node = tree
    for i, segment in enumerate(segments):
        nxt = node.children.get(segment)
        if nxt is None:
            return UTF8JSONResponse(
                status_code=404,
                content={
                    "error": "not_found",
                    "missing": segment,
                    "valid_path": node_addr(node),
                    # Names in the address-tail form (leaf `X.md`, directory `X/`).
                    # Appending to valid_path yields the canonical path directly
                    "siblings_at_parent": [
                        node_addr(c).rstrip("/").rsplit("/", 1)[-1] + ("/" if c.is_dir else "")
                        for c in node.children.values()
                    ],
                },
            )
        node = nxt
    return UTF8JSONResponse(
        status_code=500,
        content={
            "error": "internal",
            "message": "path は解決できたのに見つからない扱いになった (サーバ側の不整合)",
        },
    )


@app.post("/doc/list")
def doc_list(req: LookupRequest) -> Any:
    tree: Node = app.state.tree
    resolved = _resolve_req(req.path, tree)
    if isinstance(resolved, UTF8JSONResponse):
        return resolved
    internal, node = resolved
    if node is None:
        logger.warning("doc.list path=%r → not_found", req.path)
        return _not_found(internal, tree)

    response = serialize(node)
    response["children"] = [
        _with_addr(c, child) for c, child in zip(response["children"], node.children.values())
    ]
    response = _with_addr(response, node)
    child_paths = [c["path"] for c in response["children"]]
    logger.info(
        "doc.list path=%r → path=%s title=%r children=%d %s",
        req.path,
        response["path"],
        response["title"][:60],
        len(child_paths),
        child_paths,
    )
    return response


@app.post("/doc/grep")
def doc_grep(req: GrepRequest) -> Any:
    """Return the nodes in the subtree that match query.

    Each node's 3 fields title / description (meta.description) / body are evaluated
    independently; a node with 1+ hits in any of them is returned as 1 entry. Simultaneous hits
    in several fields of one node are broken down in match_count. Case-insensitive + NFKC normalized.
    regex=True switches to Python re pattern matching.

    path accepts any of "/" / "" / "*" / "/*" to mean the whole root subtree.

    Results are sorted by **relevance, descending** (title×10 + description×5 + body hit count,
    ties by path ascending), then [offset, offset+limit) is returned. `has_more` / `next_offset`
    let the caller continue. The old behavior of returning the first N in tree order was dropped
    because the most relevant document silently fell off.

    Only when the literal match is 0 hits and the query splits into 2+ terms does it **fall back
    to loose (term segmentation + BM25)**. The mode is always announced in the response `mode`;
    for loose, the terms used (`terms`), the terms that missed (`absent_terms`) and a `note` are
    attached — so the model does not confuse "not found" with "found another way".
    No extra arguments (automatic). No fallback when regex=True.
    """
    tree: Node = app.state.tree

    if not req.query:
        return UTF8JSONResponse(
            status_code=400,
            content={"error": "bad_query", "message": "query が空"},
        )
    # Normalize root aliases of path
    raw_path = req.path
    norm_path = "/" if raw_path in ROOT_PATH_ALIASES else raw_path

    resolved = _resolve_req(norm_path, tree)
    if isinstance(resolved, UTF8JSONResponse):
        return resolved
    internal, node = resolved
    if node is None:
        logger.warning("doc.grep path=%r → not_found", req.path)
        return _not_found(internal, tree)

    try:
        matches, total = grep_subtree(
            node, req.query, regex=req.regex, limit=req.limit, offset=req.offset
        )
    except ValueError as e:
        msg = str(e)
        if msg.startswith("bad_regex:"):
            return UTF8JSONResponse(
                status_code=400,
                content={
                    "error": "bad_regex",
                    "message": msg[len("bad_regex:") :].strip(),
                },
            )
        return UTF8JSONResponse(
            status_code=400,
            content={"error": "bad_query", "message": msg},
        )

    # Literal match 0 hits → split into terms and search again. If it splits into only 1 term,
    # loose would give the same result as exact, so do not fall back and return 0 plainly
    # (claiming "searched another way" with identical content is the worst outcome).
    mode = "exact"
    terms: list[str] = []
    absent: list[str] = []
    ignored: list[str] = []
    if total == 0 and not req.regex:
        candidate = segment_query(req.query)
        if len(candidate) >= 2:
            mode = "loose"
            terms = candidate
            matches, total, absent, ignored = grep_subtree_loose(
                node, terms, limit=req.limit, offset=req.offset
            )

    matches = [_with_addr(m, navigate(tree, m["path"])) for m in matches]
    next_offset = req.offset + len(matches)
    response: dict[str, Any] = {
        "path": node_addr(node),
        "query": req.query,
        "regex": req.regex,
        "mode": mode,
        "match_count": total,  # total number of matched nodes (not the count in this response)
        "offset": req.offset,
        "returned": len(matches),
        "has_more": next_offset < total,
        "matches": matches,
    }
    if next_offset < total:
        # The response itself carries how to continue (the model need not compute the offset)
        response["next_offset"] = next_offset
    if mode == "loose":
        # Always announce the fallback, its method and the terms used. If the search is silently
        # swapped, the model misreads it as "the query existed literally" and builds citations on it.
        response["terms"] = terms
        response["absent_terms"] = absent
        response["ignored_terms"] = ignored
        response["note"] = loose_note(terms, absent, ignored)
    logger.info(
        "doc.grep path=%r query=%r regex=%s mode=%s terms=%s → total=%d returned=%d",
        req.path,
        req.query,
        req.regex,
        mode,
        terms,
        total,
        len(matches),
    )
    return response


@app.post("/doc/read", response_class=PlainTextResponse)
def doc_read(req: ReadRequest) -> Any:
    tree: Node = app.state.tree
    resolved = _resolve_req(req.path, tree)
    if isinstance(resolved, UTF8JSONResponse):
        return resolved
    internal, node = resolved
    if node is None:
        logger.warning("doc.read path=%r → not_found", req.path)
        return _not_found(internal, tree)
    # Return the artifact body as is (deployment-value substitution and the setext workaround are done by build.py).
    body = node.body
    logger.info(
        "doc.read path=%r → path=%s title=%r body=%d chars",
        req.path,
        node.path,
        node.title[:60],
        len(body),
    )
    # The node's canonical address is returned in a header (the body stays the artifact as is; this
    # shows the model the canonical form even when the request used the old form or omitted .md).
    # Headers are latin-1, so the value is percent-encoded (= same as the HTML href). The tool unquotes it for the model.
    return PlainTextResponse(body, headers={"X-Doc-Path": node_href(node)})


# ---------------------------------------------------------------------------
# Public route: GET / HEAD /ext/doc/<addr> (for browsers; via the nucllei proxy)
# ---------------------------------------------------------------------------
#
# The address is the read_doc path itself (canonical: leaf `X.md`, directory `X/`).
#   - canonical → that node's HTML (nodes with children: body + child listing)
#   - alternate forms the tool path accepts (leaf without `.md`, directory without trailing `/`,
#     `X/_index.md`; walker.addr_to_path) → 302 to canonical. Keeps one address per page
#     (citation numbers are counted by the URL string). The browser carries `#heading` /
#     `#:~:text=` over to the redirect target. The query is re-attached
#   - anything else is 404
# Raw md is not served (the sandbox has the real file at the same path). No alias for the old
# `/ext/cage/…` either (switched before production rollout, 2026-10-06).
# ETag is the hash of the response body (304 if If-None-Match matches).

_MD = make_md()
_NO_CACHE = "no-cache"


def _public_response(request: Request, body: str, media_type: str, status: int = 200) -> Response:
    data = body.encode("utf-8")
    headers = {"Content-Security-Policy": PUBLIC_CSP, "Cache-Control": _NO_CACHE}
    if status == 200:
        etag = '"' + hashlib.sha256(data).hexdigest()[:32] + '"'
        headers["ETag"] = etag
        inm = request.headers.get("if-none-match", "")
        if etag in [t.strip() for t in inm.split(",")] or inm.strip() == "*":
            return Response(status_code=304, headers=headers)
    return Response(content=data, status_code=status, media_type=media_type, headers=headers)


def _doc_not_found(request: Request, addr: str) -> Response:
    return _public_response(
        request,
        not_found_page(f"doc に {DOC_PREFIX}/{addr} はありません。"),
        "text/html; charset=utf-8",
        404,
    )


@app.api_route(DOC_PREFIX, methods=["GET", "HEAD"], include_in_schema=False)
@app.api_route(DOC_PREFIX + "/{addr:path}", methods=["GET", "HEAD"], include_in_schema=False)
def doc_public(request: Request, addr: str = "") -> Response:
    tree: Node = app.state.tree
    if any(s in (".", "..") for s in addr.split("/")):
        return _doc_not_found(request, addr)
    # Strip only the one prefix we attached here, so `/ext/doc/ext/doc/…` is not accepted
    internal = addr_to_path(DOC_PREFIX + "/" + addr)
    node = navigate(tree, internal)
    if node is None:
        return _doc_not_found(request, addr)
    if request.scope["path"] != node_addr(node):  # scope path is already percent-decoded
        query = request.url.query
        return Response(
            status_code=302,
            headers={
                "Location": node_href(node) + (f"?{query}" if query else ""),
                "Cache-Control": _NO_CACHE,
            },
        )
    page = render_node(tree, split_path(internal), _MD)
    return _public_response(request, page, "text/html; charset=utf-8")
