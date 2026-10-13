# LLENS — read-only reference snapshot

This repository is a frozen, read-only reference snapshot of selected source files of LLENS
(Large Language Enhanced Nexus System), the on-premises LLM assistant platform developed by
PRISM-HU at Hokkaido University Hospital. It accompanies a manuscript on LLENS and exists so that
the design and the evaluation code described there can be read. **It is not a runnable distribution**: the platform runs only
together with the hospital's Web frontend (a fork of Open WebUI v0.6.5, not included here) and
with services that exist only inside the hospital's closed network.
Nothing here will be updated.

## Contents

| Directory | Component |
|---|---|
| `docs/` | Design concepts, one per file (see below) |
| `cage/` | Document service (code name cage): the HTTP gateway behind the three document tools, its tests, and the ingestion scripts and manifests for the public corpora (`cage/scripts/`) |
| `nucllei/tools/` `nucllei/functions/` | Tools and functions loaded into the Web frontend (document tools, sandbox, audio, file conversion, monitoring) |
| `nucllei/skills/` `nucllei/prompts/` | Skill library and system prompts shown to the model |
| `evals/` | Evaluation harness (`harness/`), run scripts and the tasks: the hospital document QA benchmark (`tasks/docqa/`, with its design in `SPEC.md`, the 180 frozen questions used in the paper in `questions/` and the prompts and skills given to the model in `prompts/`) and the public licensing-exam benchmarks used for model selection |
| `scripts/llm/` | SGLang launch scripts per model |

## Design documents

| | |
|---|---|
| `docs/01_architecture.md` | One host inside the closed hospital network and its components |
| `docs/02_agent_harness.md` | System prompt, skills, document tools, sandbox, limits, subagents |
| `docs/03_knowledge_layer.md` | The path-addressable document tree and why there is no vector index |
| `docs/04_doc_tools.md` | Contracts of `list_doc` / `grep_doc` / `read_doc` |
| `docs/05_sandbox.md` | nsjail code sandbox and its mounts |
| `docs/06_airgap_updates.md` | Updating a deployment that has no network access |
| `docs/07_evaluation_design.md` | Design of the hospital document access evaluation |
| `docs/08_model_selection.md` | How the model and the serving configuration were chosen |

Internal identifiers such as addresses, hostnames, extension numbers and contact details were
redacted (`<redacted-...>` placeholders); nothing else was altered.

Code comments and model-facing prompts and skills are in Japanese by design; the documentation is
in English.

## License

This snapshot (code and documentation) is released under CC BY 4.0 (`LICENSE`). It is a
read-only reference, not software intended to be run; attribution is to PRISM-HU, Hokkaido
University, and the paper once published. The public source documents referenced by the
ingestion manifests (ministry and society PDFs) remain under the terms of their publishers.
