# cage — document service

`cage` is the development code name of the document service of LLENS: a small HTTP gateway
(FastAPI) that serves a path-addressable corpus of hospital documents and clinical guidelines to
the Web frontend's tools. The model never sees the code name; the corpus is exposed as `doc`.

- The corpus is a tree of Markdown files with front matter under `data/doc` (one node = one file,
  directory metadata in `_index.md`). At start-up the service compiles it into `build/doc`, walks it
  once into memory, and fails fast on schema errors; there is no vector index.
- Three read-only endpoints back the three model-facing tools: `POST /doc/list` (children of a
  node, with `body_chars` and `child_count`), `POST /doc/read` (the Markdown body of a node) and
  `POST /doc/grep` (full-text search in a subtree, relevance-ordered, with a BM25 fallback that is
  labelled `mode: "loose"`). A wrong path returns a structured `not_found` with the valid prefix
  and the sibling names so that the model can correct itself.
- The same path string is the tool argument, the file path under the sandbox's read-only mount
  (`/ext/doc/...`) and the URL of the HTML page rendered by `GET /ext/doc/...`, so a citation is
  an address that a reader can open.
- `mon.py` serves named Prometheus queries (GPU and serving metrics) to an administrator-only tool.

Run: `uv sync && uv run cage` (listens on `localhost:13303` by default; `uv run pytest` runs the
tests). `scripts/` holds the ingestion scripts and manifests for the public corpora (medical fee
schedule, DPC, neurology and dermatology guidelines, CTCAE, cancer registry, lung-cancer guideline).

**The document data (`data/`) is not included in this snapshot**, so the service starts with an
empty tree. The design is described in `../docs/03_knowledge_layer.md` and `../docs/04_doc_tools.md`.
