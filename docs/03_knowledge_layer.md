# 03. Knowledge layer — a tree of documents addressed by path

The knowledge layer is a path-addressable tree of Markdown files, not a vector index. Each domain is a directory tree; each directory has an index node (`_index.md`) that states the scope of its subtree and declares, with a closed schema, the front-matter attributes of its children; each leaf is one "errand" with provenance in its front matter. The document service reads the tree once at start-up, keeps it in memory and exposes it through three read-only tools. Files and tools were chosen over a vector store for auditability, revision handling and air-gap portability.

## 1. Shape of the tree

```
/ext/doc/                                  ← root
├── <domain>/                              ← clinical practice guidelines (診療ガイドライン) / medical fee schedule (診療報酬) / hospital manuals (院内マニュアル) / HIS (医療情報システム) / clinical scales (臨床スケール) / cancer registry (がん登録) …
│   ├── _index.md                          ← scope and table of contents of the domain
│   └── <document><year>/                  ← one corpus (e.g. LungCancer2025_NSCLC)
│       ├── _index.md                      ← coverage, edition, source, attribute schema of the children
│       ├── CQ1.md                         ← unit file (1 node = 1 errand)
│       └── …
```

- **FS = data model**: the arrangement of files and directories is the meaning, and the API is a pure projection of it. 1 node = 1 file (`.md` with a body, `.yaml` without); a directory's own metadata is `_index.md`.
- **A recursive tree of arbitrary depth**: leaves and branches differ only in whether a node has children, and intermediate nodes can have a body (coverage, table of contents, general rules).
- **Folders are cut at the granularity meaningful for navigation**, not to mirror the source's chapters: the body should be reached in two moves, `list_doc` → `read_doc`.

## 2. Granularity: 1 node = 1 errand

A leaf is the unit a user reads for one errand: a clinical question of a guideline, an item of the fee schedule, a section of a manual, an adverse event of CTCAE, one clinical scale. Recommendations, procedures, points and numbers are in **the body of a leaf**; intermediate bodies hold coverage, table of contents and general rules, and the model is taught not to compose an answer from a table of contents. Domains where questions integrate scattered information are cut large enough to be read whole; domains of local fact lookups are cut per section.

**Doc versus skill**: a corpus that must be covered in full and whose needed slice cannot be decided in advance (all CQs, all AEs, all fee-schedule items) goes in doc; a small set of frequently used references that fits in the context at once goes in a skill.

## 3. `_index.md` and the attribute schema

Each directory's `_index.md` has a **body** stating what the subtree contains, in what order, with edition, source and retrieval date, and a **`fields:`** block, a **closed schema** governing the front matter of the direct children.

```yaml
# Example: the _index.md of some corpus
title: XX Clinical Practice Guideline 20XX
fields:
  guideline: str      # required, shown in list
  section?: str       # required, not shown in list (provenance)
  retrieved_at?: date
```

- **closed and all required**: an undeclared key, or a missing declared key, in a child is a start-up failure. `?` is only about visibility: the field is validated but not included in the `meta` of `list_doc`, and since `read_doc` returns only the body, `?` fields are pure provenance.
- **Types are primitive only** (`str` / `int` / `date` / `bool` / `list`); the meaning of values is not validated. The schema applies to direct children only, with no inheritance and no central schema.

Values are deliberately **not normalised**: strength of recommendation, evidence level, effective date and amendment history stay as natural text in the body, and interpretation is left to the model. What is structured is only the minimal identity needed for navigation: unmarked fields surface in `list_doc` (title, edition); the body surfaces in `read_doc` (official name, chapter mapping, general rules); `?` fields surface in neither (source URL, retrieval date, pages); facts constant across a corpus (publisher, licence) are hoisted to the parent node. At the end of a leaf's body sit a link to the original PDF (with page) and links to images in which figures and tables were reproduced with a VLM, so a citation is a path accompanied by edition, section and page that a reviewer can open directly.

## 4. Naming and revision

A domain name is `<subject><year>`, split by chapter as `<subject><year>_<chapter>` only when needed. A new edition is a new domain and **old editions are deleted** (no supersedes / active mechanism), with one exception that must coexist because the applicable case periods differ. Domain splitting follows how users ask. Path segments contain no whitespace and no ASCII characters that break URLs or Markdown, since the path is used directly as URL and citation.

## 5. Two ingestion methods

- **A. Structured source** (HTML, DB, CSV, xlsx): a source-specific parser drops it into intermediate JSON, from which the Markdown is generated.
- **B. PDF with no machine-readable table of contents**: a human writes one YAML manifest per document (page boundaries, splits within a page, removal of page numbers, replacement of symbol fonts, single-spot patches), and the build reads only the manifest, making no judgements.

In both, the body follows the source's notation and the procedure is recorded per corpus so it can be reproduced.

## 6. Start-up validation → runtime guarantee

The document service walks the tree once at start-up and builds it in memory. Id collisions among siblings, front-matter parse failures and schema violations are **start-up failures**, which guarantees that if the service started, everything is consistent; there is no runtime checking, and a data update is a file replacement plus a restart. The served output is compiled from the source at start-up; the transformation is only placeholder substitution (`{{DOC_BASE}}` → `/ext/doc`) and a guard against Markdown setext headings, and paths are never guessed from the body. **The tools, the sandbox and the browser all see the same output.**

## 7. Why there is no vector index

1. **Auditability**: a citation is a path with edition, section and page that a reviewer can open, and the route to it remains as the record of tool calls. With chunk retrieval it is hard to pin down "chapter N, section X".
2. **Revision = file replacement**: tracked like a code commit, with no re-embedding.
3. **Easy to carry into the closed network**: one process reading a directory, and the data is a single tar; no separate database to maintain.
4. **Context preservation**: chunking does not sever the body from its grounds or break tables; the model fetches entries that are logical units.
5. **Matched to what the model is good at**: listing, searching and reading files are abundant in training data; if a search misses the model rephrases, and since only existing paths can be referenced, hallucination is suppressed.

The cost is the upfront Markdown conversion and the dependence on the quality of the catalogue (titles, `_index` bodies); the method yields nothing on an unorganised document set, so the decision includes **investing in document organisation rather than in more sophisticated retrieval**. A second cost is that, without an index, scale dependence hides in the scores: in the BM25 fallback of `grep_doc`, common terms are dropped by the IDF ratio within the query rather than by corpus document counts, document-length normalisation is taken per direct-child group, and the variance of existing queries' rankings with respect to scale is measured when a corpus is added.
