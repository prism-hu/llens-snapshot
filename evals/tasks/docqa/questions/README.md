# docqa — the frozen question set

The 180 questions used in the paper's evaluation (6 domains × 30), as frozen before any configuration was run
(runs of 2026-09-09 to 2026-09-11). One file per domain, one question per line (UTF-8 jsonl):

| file | questions |
|---|---:|
| `shinryo_hoshu` | 30 |
| `dpc` | 30 |
| `neuro_gl` | 30 |
| `thoracic_gl` | 30 |
| `ctcae` | 30 |
| `hospital_manual` | 30 |

Fields, in the order written:

| key | meaning |
|---|---|
| `id` | `<domain>-<nnn>`; 001–020 are the first batch, 021–030 the second |
| `domain` | one of the six domains above (`hospital_manual` covers the medical safety manual, the infection control manual and the HIS FAQ) |
| `qtype` | `lookup`, `combine`, `filter`, `absent`, `figure` or `version` (definitions in `../SPEC.md`) |
| `question` | the question as sent to the model, in Japanese |
| `answer` | the reference answer (human-readable; may include a short note in parentheses) |
| `answer_kind` | how `grade.py` matches the answer: `number`, `term`, `list`, `bool` or `absent` |
| `accept` | accepted spellings or elements; empty for `absent` |
| `gold_paths` | logical paths of the evidence nodes in the frozen document tree (`/<category>/<item>`); empty when the answer is "not included" |
| `gold_pages` | page numbers in the source PDF for hospital-manual questions; otherwise empty |
| `needs_sandbox` | `true` when the question was written to require counting or filtering over files (the `filter` type) |

The authors' working notes on each question (`rationale`, `author_check`) are not part of the published set; nothing else was changed.
The prompts given to the model in the same evaluation — the common answer instructions, the collection orientation and the seven
domain guide skills — are in `../prompts/`.
