# 07. Evaluation design — in-hospital document access

The evaluation asks whether path-addressable documents plus read-only tools let the deployed model answer questions about in-hospital documents it could not answer from memory. 180 Japanese questions (6 domains × 30) were written by the development team in two batches, each labelled with one of six question types (lookup, combine, filter / aggregate, absent, figure-derived, version), given a short machine-checkable reference answer, accepted spellings and the path of the evidence node, and frozen before any configuration was run. The main comparison is A (no documents) vs B (fixed BM25 top-10 context) vs E (document tools + sandbox + domain guidance); C (sandbox only) and D (tools only) ran in the same pass and are reported in an appendix. Scoring is automatic. Reference answers were checked against the frozen corpus by script after the runs; the 18 grading corrections form a sensitivity analysis. Regeneration was allowed only on mechanistic criteria (iteration cap, service outage), never on score. The evaluation runs through **the same path as production** (the Web frontend's chat API, with skills, the tool loop and the sandbox running on the server), separate from the evaluations that call the model server directly.

## 1. Questions: 6 domains × 6 types

| Domain | Contents |
|---|---|
| medical fee schedule (診療報酬) | medical fee point table (points and notes in the notification vs. billing requirements in the circulars), Q&A interpretations, facility standards |
| DPC | classification, length of stay, points, coefficients |
| neurology guidelines | CQ, recommendation grade, precedence of supplements |
| thoracic oncology guidelines | CQ, strength of recommendation, evidence level |
| CTCAE | Grade definitions |
| hospital manuals (院内マニュアル) + HIS FAQ (医療情報システム FAQ) | procedures originating on the hospital intranet (the phone directory (電話番号簿) and the facility-standard filing status are not used) |

| Type | Definition | Aim |
|---|---|---|
| `lookup` | a factual query answered by reading one node | basic grounding |
| `combine` | combining two or more places, e.g. main rule + note + circular, CQ + supplement | can it walk the tree |
| `filter` | filtering or counting on **a condition the doc tools cannot express** ("how many codes have N points or more") | expected to need the sandbox |
| `figure` | values that exist only inside tables / figures reproduced by the VLM | is the figure reproduction effective |
| `version` | edition, supplement, revision date, effective date | front matter / body of intermediate nodes |
| `absent` | make the model answer that something **is not in the corpus** (a non-existent code, a society guideline not included) | hallucination suppression |

The development cohort of 180 questions breaks down as lookup 73, combine 39, filter 30, absent 18, figure 11, version 9.

## 2. Question-writing rules

- **Every answer is fixed by reading the text**, never from a generative model's memory; for `filter`, the count comes from a script that scans the actual files. Only questions whose answer is **short and unique** (number, term, yes/no, short list).
- The question is a natural sentence that clinical or administrative staff would actually ask, and **never contains a path**. Questions in each domain are spread over several sections (no two questions from one node).
- Questions hard to answer from memory are preferred, but 2–3 `lookup` questions answerable from general knowledge are mixed in so that the no-document floor is not zero.
- `absent` means "plausible but not there": non-existent editions, non-existent codes, documents outside the covered societies. Documents absent from the corpus but present on the intranet do not qualify, because the production preset can browse the hospital Web.
- No question whose answer or evidence is a personal name, phone number, extension or e-mail address.

Each question carries `question` / `answer` / `answer_kind` (number / term / bool / list / absent) / `accept` / `gold_paths` / `gold_pages` / `needs_sandbox` / `rationale`, and was **frozen before any configuration was run**. The corpus, the question set and the configuration definitions are each identified by SHA-256.

## 3. Configurations

| Configuration | What is given |
|---|---|
| **A** | no documents |
| **B** | the fixed BM25 top-10 chunks retrieved with the question text alone (2,000 characters, 200 overlap, CJK 2-gram + ASCII tokens, k1 = 1.2, b = 0.75), appended to the context once before generation. Does not pass through the platform's tools |
| C | sandbox only (read-only mount of `/ext/doc`) |
| D | document tools only |
| **E** | document tools + sandbox + corpus guidance + 7 domain skills, with the instruction to combine the document tools and code execution as needed |

- The main comparison is A / B / E; C / D ran in the same pass and only their accuracy is shown in an appendix.
- The model, the answer instruction (end with an "Answer" (【回答】) line and an "Evidence" (【根拠】) line), and the decoding settings (temperature 1.0, top-p 0.95, thinking on, output cap 16,384) are common to all configurations. The system prompt is a concatenation of blocks: answer instruction (all) + corpus guidance (C–E) + sandbox (C, E) + document tools (D, E) + the one-sentence combination instruction (E). Domain skills are short: a heading, where to start exploring, one sentence about the domain, and the corpus guidance.
- The evaluated configurations are a **subset** of the deployed harness; the deployed skill library, subagents and image input are excluded. The evaluation ran on a separate localhost-only instance started from the same container image, reading a replica of the frozen corpus and sending requests to the same model server as the deployment.
- The no-document control is built by **removing all tools and skills**; if the sandbox stayed, `run_shell` could read the raw files directly.

## 4. Execution

Each question runs with 3 seeds (3 configurations × 180 questions × 3 = 1,620 responses; 2,700 including C / D), with configuration order shuffled per question and a fresh user workspace per question, configuration and trial. Tool-call cap 90, deadline 1,800 s per question; timeouts and execution errors are kept as incorrect, never regenerated. The complete record is the server-side chat itself; result rows keep the full reasoning and the full arguments and results of every tool call.

## 5. Regeneration criteria (mechanistic, not score-based)

- A response is regenerated only for a mechanistic reason: it stopped at a lower tool-call cap (30) that was later raised to 90, or the model server refused follow-up calls. Regeneration uses the same seed, prompt and snapshot, and every original response is kept.
- A connection failure to the document service (code name cage) affected one seed; D and E of that seed were regenerated **for all questions regardless of score**, while A / B / C were kept byte-for-byte because their inputs do not depend on the service. This is disclosed as a protocol deviation.
- The final comparison uses one response per question, configuration and seed; originals and regenerations are not mixed.

## 6. Scoring

- **The primary metric is automatic matching of the final answer.** Numeric normalisation (full-width → half-width, comma removal, partial match), accepted strings, `list` is correct when all required elements are present, `absent` is judged by a fixed phrase set ("not included" (収録されていない), "cannot be confirmed" (確認できない), etc.).
- Citation correctness is "does a gold path appear in the answer text or in the arguments of `read_doc`".
- Scoring uses the current version of the question file (looked up by id), so corrections to questions take effect without re-running.

## 7. Verification of reference answers and audit of scoring

After the runs, all 180 reference answers were **checked by script against the frozen corpus** (quotation of the original text, recomputation from the table, or an absence search), with an independent second check on a subset. The automatic scoring was re-audited over all 2,700 responses; the 18 corrected judgements (0.7%) are presented as a sensitivity analysis. There is no manual review of responses.

## 8. Statistics

Correctness is averaged over 3 seeds per question before taking paired differences, making the **question the unit of analysis**. 95% intervals come from 10,000 question-level bootstrap resamples stratified by document domain, with a paired sign-flip test; the primary contrast is E − B, unadjusted. Per-type analyses are exploratory, with no adjustment across types.

## 9. Disclosure

For the hospital-manual domains **only scores** are published, neither the question text nor quotations; the question sets for the 5 public-document domains are available on request. Identifiers of the question set, prompts, tool specifications and snapshots are given in an appendix.
