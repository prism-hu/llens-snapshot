# docs — design concepts

Design concepts of LLENS, the on-premises LLM assistant platform developed by PRISM-HU at Hokkaido
University Hospital, one concept per file. These are descriptions of the design; the implementation is in the
other directories of this snapshot (`cage/`, `nucllei/`, `evals/`, `scripts/`) and in the paper.

| File | Concept |
|---|---|
| [01_architecture.md](01_architecture.md) | One host inside the closed hospital network: model server, Web frontend, document service, sandbox |
| [02_agent_harness.md](02_agent_harness.md) | The harness: system prompt, progressively disclosed skills, three document tools, sandbox, iteration and time limits, subagents. Tools fixed per model preset |
| [03_knowledge_layer.md](03_knowledge_layer.md) | A path-addressable document tree: domain, directory, unit file; `_index.md` and attribute schema; front matter; "one node = one task"; why there is no vector index |
| [04_doc_tools.md](04_doc_tools.md) | Contracts of `list_doc` / `grep_doc` / `read_doc`, the loose fallback, self-correction on not_found, one address (path = sandbox path = URL = citation), the boundary with `read_file` |
| [05_sandbox.md](05_sandbox.md) | nsjail isolation, read/write layout of `/workspace` and `/ext`, imports / exports / trash, file exchange, documents read-only |
| [06_airgap_updates.md](06_airgap_updates.md) | Updates into the closed network: four categories (images / codes / deps / docs) plus a manifest on physical media; models replaced during planned downtime |
| [07_evaluation_design.md](07_evaluation_design.md) | Hospital document access evaluation: 6 domains x 6 question types, frozen corpus, configurations A / B / E (C / D in the appendix), automatic matching, scripted reference checks |
| [08_model_selection.md](08_model_selection.md) | Model selection axes (licensing-exam benchmarks, memory headroom, image input, speed, licence) and the serving configuration (EAGLE-3, context length, concurrency) |

Terminology: the document service is referred to by its development code name cage, and the Web
frontend is a fork of Open WebUI v0.6.5 (code name nucllei). The texts say "document
service" and "Web frontend".
