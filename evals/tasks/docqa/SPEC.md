# docqa — in-hospital document QA benchmark

Measures the effect of grounding the model in documents through the doc tools (`list_doc` / `grep_doc` / `read_doc`) and the sandbox.
**Requests go through the chat-wire API of the Web frontend (code name nucllei), i.e. the same path as production** (skill, tool loop and sandbox run server-side)
(`evals/harness/nucllei_client.py`). The other tasks call the inference server directly; this one does not.

The paper's evaluation extends this design (more questions, more configurations, repeated seeds); see `docs/07_evaluation_design.md`.

## Conditions

| Preset (model id) | Meaning |
|---|---|
| `llens-no-tool` | Control. No tools and no skills; the same system prompt with the sections that describe document reading, the sandbox and the doc tools removed. The model answers from memory alone |
| `llens-thinking` | Production preset. Doc tools + index skill + sandbox (`/ext/doc` and `/ext/doc-static` mounted read-only) |

Thinking is on in both conditions (the model's recommended setting); a thinking-off variant is not evaluated.
Temperature and other sampling settings are the preset defaults (as deployed). N=1, per-question bootstrap CI.

## Domains and size (120 questions = 6 domains × 20)

| domain | Corpus location | Notes |
|---|---|---|
| `shinryo_hoshu` | medical fee schedule (診療報酬): `診療報酬/医科点数表R08_{基本診療料,特掲診療料}` + `疑義解釈R08` + `施設基準R08_*` | distinguishing the notification (points, notes) from the circular (billing requirements) |
| `dpc` | `診療報酬/DPC点数表R08` | classification, length-of-stay periods, points, coefficients |
| `neuro_gl` | clinical practice guidelines (診療ガイドライン): `診療ガイドライン/神経疾患/*` (27 GL) | CQ, recommendation grade, precedence of supplements |
| `thoracic_gl` | `診療ガイドライン/{肺癌,中皮腫,胸腺腫瘍}2025_*` | CQ, strength of recommendation, evidence level |
| `ctcae` | `診療ガイドライン/CTCAEv5_JCOG_2025` | Grade definitions |
| `hospital_manual` | hospital manuals (院内マニュアル): `院内マニュアル/{医療安全管理マニュアル,感染対策マニュアル}` (medical safety manual, infection control manual) + `医療情報システム/*` (HIS FAQ (医療情報システム FAQ)) | from the hospital intranet. **The phone directory (電話番号簿) and the facility-standard filing status are not used** |

## Question types (breakdown of the 20 questions per domain)

| qtype | Count | Definition |
|---|---:|---|
| `lookup` | 8 | Fact retrieval answerable by reading one node (points, Grade definition, recommendation grade, a value in a procedure) |
| `combine` | 4 | Combines two or more nodes, or several places in one node (main rule + note + circular, CQ + supplement) |
| `filter` | 3 | Filter or count by a **condition that cannot be expressed in the doc tools** ("how many codes have N points or more", "classifications whose period II is 30 days or longer"). Expected to need the sandbox. `needs_sandbox: true` |
| `figure` | 2 | A value that exists only inside a table or figure reproduced by VLM (overlay / fig) |
| `version` | 1 | Edition, supplement, revision date or effective date ("did the latest supplement change the recommendation", "what is the revision date") |
| `absent` | 2 | Asks the model to say that something **is not in the corpus** (a real but unincluded document, a non-existent code, etc.). Measures hallucination suppression |

## Question file

`evals/tasks/docqa/questions/<domain>.jsonl`, one question per line, UTF-8, keys in the order below. The frozen files used in the paper are in `questions/` (without the `rationale` and `author_check` working notes).

```json
{"id": "shinryo_hoshu-001",
 "domain": "shinryo_hoshu",
 "qtype": "lookup",
 "question": "<question text>",
 "answer": "<answer>",
 "answer_kind": "number",
 "accept": ["<accepted spelling>"],
 "gold_paths": ["/診療報酬/医科点数表R08_基本診療料/A000_初診料"],
 "gold_pages": [],
 "needs_sandbox": false,
 "rationale": "<rationale (working note, not published)>",
 "author_check": false}
```

- `question`: a natural sentence that clinical or administrative staff would actually ask. **Never contains a path.** Document names and, where needed, the fiscal year or edition may be given
- `answer`: the human-readable answer (short)
- `answer_kind`: `number` / `term` / `bool` / `list` / `absent`
- `accept`: used for grading. `number` is ASCII digits only (no unit, no commas; the grader converts full-width digits, strips commas and matches substrings).
  `term` lists spelling variants (`["トリプタン", "triptan"]`). `bool` is a word group equivalent to `["はい"]` or `["いいえ"]`.
  `list` is a list of elements that must **all** be present. `absent` may be empty (the grader uses a fixed word group meaning "not included / not found / cannot be confirmed")
- `gold_paths`: doc paths of the evidence nodes (leaves), relative to the corpus root, without extension (`_index` means the parent directory).
  Empty for `absent`. A citation is correct when any gold path appears in the answer text or in the arguments of `read_doc`
- `gold_pages`: optional (only for nodes that carry `【p.N】` markers)
- `rationale`: a one-line quotation of the evidence (for author verification; not published)
- `author_check`: true once the author has verified the question by eye (false when generated)

## Question-writing rules

- **The answer is always fixed by reading the text.** Never from a generative model's memory. For `filter`, the count comes from a script that scans the actual files, and the method is recorded in `rationale`
- Only questions whose answer is **short and unique** (number, term, yes/no, short list). No free text
- **No question whose answer or evidence is a personal name, phone number, extension, PHS number or e-mail address.** Contact pages of the infection control and medical safety manuals, and reviser names in revision histories, are avoided
- Questions hard to answer from memory are preferred (current-year points, hospital-specific procedures, the mapping between CQ number and recommendation grade). However, 2-3 `lookup` questions answerable from general knowledge are mixed in so that the no-tool floor is not zero
- The 20 questions of each domain are spread over several sections and chapters (no two questions from one node)
- `absent` uses things that are "plausible but absent": a non-existent code (`A000-9`), an unincluded society guideline, an unincluded chapter, a CQ that exists only in an older edition, etc. The point is that the answer **does not fabricate a value**
- Only things that do not exist on the hospital intranet either can be used for `absent`: the production preset can reach intranet originals with `fetch_url`, so a document that is on the intranet but not in the corpus does not work as `absent` (the model fetches the original and answers correctly)
- When the answer can be a single character ("A", "3"), the bare value is also included in `accept` (grading matches inside the 【回答】 block)

## Running and grading

- `uv run python -m evals.tasks.docqa.run --model llens-thinking [--domain X] [--limit N] [--resume]`
  → one jsonl per run, one line per question (question id, answer text, tool_calls, usage, timing, error). The model is asked to end its reply with a 【回答】 (answer) line and, if any, a 【根拠】 (evidence) line with the document path
- `uv run python -m evals.tasks.docqa.grade <results.jsonl>` → accuracy / citation accuracy / tool calls / time, aggregated by domain × qtype (Markdown table).
  `--compare a.jsonl b.jsonl` gives the difference between two conditions with a per-question bootstrap 95% CI and McNemar's exact test.
  **Grading uses the current version of `questions/`** (accept / answer_kind / gold_paths / qtype / domain looked up by id; `--questions DIR` to change),
  so corrections to questions take effect without re-running. The snapshot stored in the results is only a fallback when the id is missing
- Results rows carry `reasoning` (full text) and `content_blocks` (the raw block sequence of the persisted message, including tool-call arguments and full tool results).
  The complete record is the server-side chat itself; `uv run --group evals python -m evals.tasks.docqa.dump_chats <results.jsonl> [--executions] [--force]` saves each chat to `<results dir>/chats/<question id>.json`. Chats are deleted from the server (`--delete-chats`) only after they have been saved

## Treatment in the paper

- Main result: accuracy and citation accuracy of the two conditions over 120 questions (Table). Breakdown by domain × qtype (Supplementary)
- For the hospital-manual domain **only scores** are published; neither the question text nor quotations from the text. The question sets of the 5 public-document domains are available on request
- Figure: screenshots of representative questions
