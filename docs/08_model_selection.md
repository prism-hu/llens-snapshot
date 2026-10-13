# 08. Model selection and serving configuration

The platform (host + SGLang + the Web frontend (code name nucllei)) is fixed; the model is the part expected to change, so each candidate is compared on the same axes and can be switched in. Candidates are open-weight models that fit 8 × H200 NVL with headroom, used as distributed (quantised weights as is; no in-house quantisation), and compared on five axes: Japanese medical benchmark score (the two most recent national medical licensing examination (医師国家試験) sets), memory headroom for concurrent requests (KV cache after weights), native image input, decode speed, and licence. The served configuration adds EAGLE-3 speculative decoding (lossless by construction; the draft only affects speed), a 262,144-token context and 16 concurrent requests. Per-model SGLang flags are deliberate tuning and are kept with the model.

## 1. Selection axes

| Axis | What is measured | Why |
|---|---|---|
| licensing-exam benchmark | the two most recent national medical licensing examinations (400 questions each, weighted 500 points). The provider's prompt and answer extraction are used; run with and without thinking, temperature 0, one response per question | aligns the floor of Japanese medical knowledge across candidates. The latest sitting is primary to avoid contamination from training data |
| memory headroom | KV-cache capacity left after loading the weights | determines the number of concurrent requests and the maximum context. Even a trillion-parameter class model fits weights + ample KV on 8 GPUs if distributed as INT4 |
| image input | can it accept images natively | show the model itself PDFs without a text layer, figures and forms (no separate VLM) |
| decode speed | output token rate at 4 / 8 / 16 concurrent, and an input-length sweep | perceived responsiveness in dialogue, and the duration of long tool loops |
| licence | permits in-hospital use and modification | — |

The candidates compared are Kimi K2.6, GLM-5.1 and DeepSeek V3.2; Kimi K2.6 (about 1 T total parameters, 32 B active, native INT4 QAT, MLA, 262K max, image input) is the main model. VRAM does not allow running candidates in parallel, so operation is by switching.

## 2. Serving configuration

```
--tp 8                      tensor parallel over 8 GPUs
--context-length 262144     the model's maximum context
--max-running-requests 16   concurrent requests (from KV headroom)
--mem-fraction-static 0.87  fraction reserved statically for weights + KV
--reasoning-parser / --tool-call-parser   matched to the model
--speculative-algorithm EAGLE3            draft model (MLA variant) + steps 3 / topk 1 / draft tokens 4
```

**Thinking / Instant**: the model defaults to thinking ON. One SGLang instance serves both, so the Web frontend registers the same endpoint twice and the presets differ only in sampling settings and the chat-template argument that disables thinking. The model server is protected by an API key.

## 3. EAGLE-3 speculative decoding

- A small draft model looks several tokens ahead, and the main model verifies them together in one forward pass. Only tokens matching the main model's output distribution are accepted, so **the output is mathematically identical to the main model alone (lossless)**; draft quality affects only how many tokens are accepted, i.e. speed.
- EAGLE-3 feeds the main model's intermediate hidden states to the draft, so the draft is **specific to its target model**; the adopted draft was continually trained with the main model as target.
- The main model uses MLA (Multi-head Latent Attention), so the MLA variant of the draft is used; the MHA draft goes through an attention-backend branch that fails at long context, confirmed with a single 221K-token prompt.
- VRAM cost is under 1% of the main model, and at low to medium concurrency decode speed only moves upward.

## 4. Updating the inference environment

The lock of the inference environment (SGLang + torch + CUDA) is not a default kind of the air-gap delivery. It is heavy (about 4 GB), and swapping it under a running SGLang breaks unpredictably, so like the model weights it is applied **only during a planned downtime** (→ `06_airgap_updates.md`).
