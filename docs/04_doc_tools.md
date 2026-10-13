# 04. Document tools — the contract of `list_doc` / `grep_doc` / `read_doc`

Three read-only tools expose the document tree: `list_doc(path)` returns a node and its direct children, `grep_doc(query, path)` returns ranked matches in a subtree and declares when it has fallen back to tokenised BM25, and `read_doc(path)` returns the body with a first line `path: <canonical>` for citation. A wrong path is not an exception: the error carries the valid parent and its children so the model can self-correct. One address string (`/ext/doc/...`) serves as the tool `path`, the sandbox file path, the page URL and the citation.

## 1. The contract

| Tool | Input | Returns | Design constraints |
|---|---|---|---|
| `list_doc(path)` | Path of a node (root if omitted) | `{path, title, meta, body_chars, child_count, children: [{path, title, body_chars, child_count}]}` | **No recursion**, no body. `body_chars` (0 = no body) and `child_count` tell the model, before reading, whether a read is worthwhile and how much would come back |
| `grep_doc(query, path, regex, offset, limit)` | Search term and scope | matches in descending relevance (`path, title, body_chars, child_count, match_count{title, description, body}, body_first_match_snippet`), the total, `has_more` / `next_offset` | **Scope the subtree with `path`**. Only the ranked top results (default 10, max 50); the snippet is the first hit ±40 characters, and the body is fetched with `read_doc` |
| `read_doc(path)` | Path of a node | First line `path: <canonical>` + the body Markdown; empty for a node without a body | Links to figures and originals are returned as relative URLs (no origin is added) |

**Structure and content are separated** (`list_doc` is structure, `read_doc` is content), so the model does not drown in payload, and the tree is cut so that the body is reached in two moves. Every HTTP endpoint is `POST + JSON body` (Japanese paths are not encoded into GET query strings), and JSON is not ASCII-escaped, since turning Japanese into `\uXXXX` more than doubles the tokens.

## 2. Design of `grep_doc`

- **exact is the default**: the whole query is matched as one substring (NFKC-normalised, case-insensitive) independently against title / description / body; the fields that hit are broken out in `match_count`. Relevance is title × 10 + description × 5 + body hit count; with a single-term substring BM25 would only scale the IDF by a constant, so it is used only as the fallback.
- **Tokenised fallback (loose)**: only when the literal match has zero hits and the query splits into two or more terms, the search is redone with term splitting + BM25, automatically. Splitting uses no dictionary; it cuts at punctuation, whitespace and changes of script (kanji / hiragana / katakana / alphanumeric), which is coarser than morphological analysis but **does not break unknown words such as drug names**.
- **No silent substitution**: the response's `mode` always declares `"exact"` / `"loose"`, and in loose mode it carries `terms`, `absent_terms` (no hit anywhere in this subtree), `ignored_terms` (dropped as too common), `note` (statement of fact + next step) and `matched_terms` per entry. Without the declaration the model misreads the result as a literal hit and builds citations on it; `absent_terms` is the fact most likely to lead to action, so it comes first in the note.
- **Excluding common terms**: terms whose IDF is below 50% of the query's maximum IDF are dropped. A criterion based on corpus document counts shifts rankings whenever an unrelated corpus is added; the within-query ratio is less sensitive to scale (at 4 scales the spread of rankings shrank from up to 888 positions to 1).
- **Two kinds of note for a miss**: an absent term containing kana or alphanumerics (drug names, proper nouns) suggests the document is not in the corpus; a kanji-only compound suggests the split is coarse, so split shorter and retry. A uniform warning produced false alarms in 20% of cases.

## 3. Siblings on not_found (self-correction)

A non-existent path is returned as a structured error:

```json
{"error": "not_found", "missing": "typoCQ",
 "valid_path": "/ext/doc/<domain>/<document>/",
 "siblings_at_parent": ["CQ1.md", "CQ2.md", "...", "sub/"]}
```

`valid_path` is the nearest existing parent and `siblings_at_parent` the names of its children (`X.md` for a leaf, `X/` for a directory). The frontend composes this into "that path is not in the document corpus; missing segment …; next step …", and the model picks again from the candidates instead of repeating the same wrong path. The prompt says to use a path literally as it appeared in a skill's catalogue, the children of `list_doc` or the matches of `grep_doc`, never to build one from a title; to read skill → `list_doc` → intermediate `read_doc` → leaf `read_doc` only as much as needed; to cite only nodes read with `read_doc`, quoting verbatim; and to pass very long nodes to a subagent.

## 4. One address

If the tool path, the sandbox path and the page URL were different strings, the model would have to assemble URLs itself and would drop prefixes; so there is one address, and **doc tool `path` = sandbox path = page URL = citation**:

| Node | Canonical form |
|---|---|
| root | `/ext/doc/` |
| directory | `/ext/doc/<domain>/…/` (trailing `/`) |
| leaf | `/ext/doc/<domain>/…/<name>.md` (the actual file in the sandbox) |

- **Lenient input, one output form**: the tools accept the same node regardless of the prefix, `.md`, a trailing `/`, `…/_index.md` or `#heading` (alternative spellings only; no guessing), and every path in a response is canonical. The first line `path:` of `read_doc` lets the model cite in canonical form however it called the tool: it wraps this path in `[ ]`, adding at most `#heading` or `#:~:text=a passage of the original`.
- **Browser**: a GET on the same address returns the document as HTML; heading ids are the heading text itself, so a heading seen via `read_doc` is directly usable as an anchor. Relative URLs are returned without an origin so that nothing breaks when the entry point's scheme or host changes.
- **Figures and originals** live apart under `/ext/doc-static/…`, so they do not appear in `list_doc` and are not doc nodes.

## 5. The boundary with `read_file`

The sandbox mounts `/ext/doc` read-only and has a generic `read_file`, but the two are separated as **doc = queries against a curated corpus, `read_file` = plain files**: the doc tools match per field in relevance order with NFKC normalisation and the loose fallback, survey a node's children with titles, body sizes and child counts in one call, tolerate path variants and return the valid path and children on error, and run as one HTTP call usable in presets without `run_shell`; `read_file` and `run_shell` read exactly the path as written, grep literally, and spin up a jail. `read_file` does not accept `/ext/doc/…` and directs to `read_doc`.

**Read with the doc tools, count with the sandbox**: filtering and counting by conditions the doc tools cannot express ("items with N points or more") is the sandbox's job (`rg` / pandas), because `list_doc` does not recurse and `grep_doc` returns only the top results; a path hit in the sandbox can be passed to `read_doc` as is. Everything is not moved to the sandbox because citations and figure URLs can only be taken from the tools' return values, the citation remains as an auditable path, self-correction and ranking are features of the doc tools, and the doc tools can be put on presets that have no sandbox.
