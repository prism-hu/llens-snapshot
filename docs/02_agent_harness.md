# 02. Agent harness — what surrounds the model, assembled to fit the model

The *agent harness* is the set of things the frontend places around the model for every request: a system prompt, progressively disclosed skills, three read-only document tools served by a separate process, a code sandbox, limits, and a subagent. Tools are fixed per model preset on the server side; only native function calling is used. The harness is tuned to the model actually served, and the system prompt contains only what changes model behaviour.

## 1. Elements

| Element | What it does |
|---|---|
| system prompt | User profile and response style; role boundaries (no documents with legal effect, etc.); how to express confidence and grounds; privacy; how to read attachments, use the sandbox and the document tools; the form of citations |
| skill | Progressively disclosed instruction document: the one-line description is always visible, the body is loaded only when needed. The frontmatter can name dedicated tools, which appear when the skill is loaded |
| document tools | `list_doc` / `grep_doc` / `read_doc`, read-only, served by the document service (→ `04_doc_tools.md`) |
| sandbox | `run_python` (stateful) / `run_python_once` / `run_shell` / `read_file` / `write_file`, plus the bridge between attachments and the sandbox (→ `05_sandbox.md`) |
| limits | An iteration cap on tool calls, a per-request deadline, a wall-clock limit per sandbox execution |
| subagent | A child session that reads material independently and returns only its final text; caps on nesting depth and parallelism. What to delegate is decided solely by the parent's system prompt |

## 2. Tools fixed per preset

Tools are **assigned to each model preset on the server side**, so the tool list the model sees is decided by the deployer with no per-user variation, and only native function calling is used. Access control is per preset and per group. Administrator tools (monitoring) are named in a skill's frontmatter and appear when the skill is loaded; this guarantees that whenever a tool can be called its skill body is in the context, so a tool's description only explains its arguments and the rules for choosing between tools live in the skill. Subagents run on a separate child preset with its own fixed tool set.

## 3. Progressive disclosure

A skill's description says when to load it in the user's own phrasing ("how many points is the initial consultation fee", "what are the facility criteria for this add-on"); the body holds only how to look things up in that domain, its pitfalls, and the catalogue. The document corpus has the same shape: triggered by a description, narrowed via the children of `list_doc`, scoped by the body of an intermediate node, then the leaf is read. Doc is **references too large for one skill, split into directories**; the reading order skill → list → intermediate read → leaf read is written once in the system prompt, and each skill holds only the domain-specific reasons and examples.

Skills are of four kinds: per-domain index skills (`doc-*`: coverage, where to start, terminology quirks such as sub-numbers of fee-schedule items or differing CQ numbering schemes); a cross-cutting skill (`doc-crosscut`: whether aggregation, conditional filtering and comparison across directories is done with the document tools or the sandbox); task skills (attachment conversion, huge attachments, voice, memory, recipes, the hospital intranet Web); and administrator skills (monitoring).

## 4. Assembled to fit the model

The harness is configured to the served model: the system prompt states the model's context length and the threshold for thinning attachment text follows it; the SGLang reasoning and tool-call parsers are matched to the model; because the model accepts images, PDFs without a text layer and figures are shown to the model itself without a separate VLM; KV-cache headroom sets the number of concurrent requests and the maximum context.

The system prompt contains **only statements that actually change the model's behaviour**. Enumerations of what the system can do are left out because they change nothing; what is written is only what the model would otherwise do on its own (producing legal documents, misapplying disclaimers).

## 5. The form of tool return values and failures

- Because a return value may be copied straight into the answer, a failure takes a fixed form: "**a statement of fact** (safe to relay to the user) + `next step:` (addressed only to the model)".
- Failures are raised as exceptions and the frontend composes the sentence; a returned string would be treated as a normal result even if it started with `ERROR:`.
- Kinds of failure (retryable / not found / transient / broken on our side) map to UI colours, so that red means only that our side is broken.
- Tools are small, have one role each, leave judgement to the model, and never silently switch to another behaviour.

## 6. The three forms of reference

| Argument | Meaning | Accepted form |
|---|---|---|
| `url` | A URL a browser can open; the only thing that may be pasted to the user | An absolute URL with scheme, or a same-server relative URL `/ext/...` `/api/v1/files/{id}/content` |
| `path` | The sandbox file system | `/workspace/...` `/ext/...` |
| `file_id` | Attachments and generated files | UUID |

**The argument name decides the meaning**; a tool never reinterprets a value as another argument's meaning based on its shape, and a non-conforming value yields a fixed sentence stating the accepted form. For `/ext/...` the URL and the sandbox path are the same string (→ `04_doc_tools.md`).
