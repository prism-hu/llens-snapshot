# 01. Architecture — one host inside the closed hospital network

LLENS runs on a single host inside the hospital's closed medical-information network: an inference server (SGLang, 8 × H200 NVL, tensor-parallel), a Web frontend (a fork of Open WebUI v0.6.5, code name *nucllei*) that owns the agent harness and user management, a document service (code name *cage*) that serves a path-addressable corpus through three read-only tools, and an nsjail code sandbox that mounts the same corpus read-only. The model weights, the document corpus and the skill library are each installed and updated separately from the rest. Nothing reaches the Internet after installation.

## 1. Where it sits, and the premises

The deployment target is the **closed medical-information network** that hosts the hospital information system and the electronic medical record. The institution's requirement, under the national safety-management guidelines, that patient-derived processing stay inside that network is what decided local deployment. The host cannot reach external networks, so **updates after deployment enter only on physical media** (→ `06_airgap_updates.md`); model weights are swapped during a planned outage.

## 2. Components

```
Users (browser on a hospital terminal)
      │
      ▼
  Reverse proxy (TLS termination)
      │
      ▼
  Web frontend ──────────┬──► Model server (SGLang)        inference
   (agent harness)       ├──► sandbox (nsjail)             code-execution tool
                         ├──► Document service             document tools (list / grep / read)
                         └──► STT / TTS (CPU)              voice in / out

  SGLang + DCGM exporter ──► Prometheus ──► Grafana        monitoring
```

The model server serves an open-weight MoE model (Kimi K2.6 at evaluation time: about 1 T total parameters, 32 B active, INT4 weights) with 8-GPU tensor parallelism over an OpenAI-compatible API. The document service loads the tree of hospital documents and guidelines at start-up, serves `list_doc` / `grep_doc` / `read_doc`, and serves HTML pages at the same addresses. The sandbox isolates Python / shell execution with nsjail, with a per-user `/workspace` and a read-only mount of the corpus at `/ext/doc`. Attachments (docx / xlsx / pdf …) are converted to Markdown inside the frontend process; PDFs without a text layer are shown to the model as images. STT (faster-whisper) and TTS run server-side on CPU, because of the closed network and patient voice, so as not to disturb the inference GPUs.

## 3. The parts and how each is installed

The host, SGLang, the base version of the frontend, the sandbox isolation method, the contract of the three document tools and the air-gapped update path make up the installed platform, which stays close to defaults.

Three parts are installed and updated on their own: the **model**, chosen on the national medical licensing exam benchmark, memory headroom, image input, speed and licence (→ `08_model_selection.md`); the **document corpus**, added per domain and revised by replacing files (→ `03_knowledge_layer.md`); and the **skill library and system prompt**, written for the model in use (→ `02_agent_harness.md`).

## 4. What the frontend removes from and adds to Open WebUI

Features that are unnecessary for a single closed-network institution, or that bypass the tool path, are removed: built-in RAG (embeddings and vector store), knowledge bases, cross-conversation memory, Web search, image generation, the in-browser code interpreter, and sharing with the external community.

Added are **skills** (instruction documents the model loads when needed, which can bring their own tools) and **subagents** (child sessions that read material independently and return only the final answer). Tools are **fixed per model preset on the server side** rather than picked per chat, and only native function calling is used. There is exactly one code-execution path (the sandbox) and two ways to reach a document, the document tools and the read-only mount in the sandbox, both using **the same address string** (`/ext/doc/...`, → `04_doc_tools.md`). References passed to tools are confined to three arguments, `url` / `path` / `file_id`, whose **name decides the meaning**, and tool failures come in a fixed form, "a statement of fact" + "next step", so that the model self-corrects by mechanism rather than by prohibition.

## 5. The subset used in the evaluation

The evaluation (→ `07_evaluation_design.md`) uses only the document tools, the sandbox and the per-domain guide skills; not the deployed skill library, subagents, image input, voice or monitoring. It runs on a separate localhost-only instance started from the same container image, reading a frozen copy of the corpus and sending requests to the same model server.
