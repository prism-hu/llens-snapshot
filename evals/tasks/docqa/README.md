# docqa

In-hospital document QA: author-written questions over the hospital's document corpus (fee schedule, DPC, clinical practice guidelines, CTCAE, hospital manuals), each with a short machine-checkable answer and the path of the evidence node. It measures the effect of grounding the model through the doc tools and the sandbox against answering from memory alone.

Design (conditions, domains × question types, file format, question-writing rules, grading): [`SPEC.md`](./SPEC.md). The paper's extended protocol is in `docs/07_evaluation_design.md`.

- `run.py` sends each question as one chat through the Web frontend's chat-wire API (tools, skills and sandbox run server-side), appending a request to end with a 【回答】 (answer) line and a 【根拠】 (evidence) line, and appends one jsonl row per question (answer text, tool calls, usage, timing).
- `grade.py` extracts the 【回答】 block and grades it by `answer_kind` (numeric equality, substring match for terms, all elements for lists, a fixed "not included" word group for `absent`); a citation is correct when a gold path appears in the answer or in a `read_doc` argument. `--compare` gives per-question bootstrap CIs and McNemar's test between two conditions.
- `lexical.py` is a deterministic BM25-style baseline over the corpus; `dump_chats.py` saves the server-side chats.

The 180 frozen questions used in the paper, with reference answers and evidence paths, are in `questions/`; the prompts and skills given to the model in that evaluation are in `prompts/`. The corpus and the run traces are not included in this snapshot.
