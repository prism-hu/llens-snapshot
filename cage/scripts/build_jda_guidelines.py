#!/usr/bin/env python3
"""Manifests of the guidelines published by the Japanese Dermatological Association (JDA) → doc tree
`診療ガイドライン/皮膚疾患/` (thin wrapper around build_jns_guidelines).

Just switches the corpus to `jda_guidelines` and calls the same main. **The manifest format,
the `pdfs[].url` / `patches:` / `normalize_radicals:` specs and the options are all documented
in the docstring of `scripts/build_jns_guidelines.py`, the single source** (also shown by `--help`).

Where this corpus lives:
  - `scripts/jda_guidelines/<id>.yaml`        … manifests (hand-written)
  - `cache/jda_guidelines/pdf/<file>`         … original PDFs (fetched from the society site on 2026-09-07)
  - `data/doc/診療ガイドライン/皮膚疾患/`       … output (**a section under the category `診療ガイドライン`**)
  - `data/static/jda_guidelines/{docs,figs}/<slug>…` … delivered original PDFs and figures (flat)
  - `data/ingest/jda_guidelines/`             … overlay, VLM batches, figs.json etc.

The front matter of `皮膚疾患/_index.md` has the same 4 fields as JNS (`神経疾患/`)
(guideline / publisher / source / retrieved_at). The parent `診療ガイドライン/_index.md` imposes
this as a closed schema via `fields:`, so adding or removing a field makes the walker fail
to start (docs/cage/01_brief.md "front matter schema").

Shapes specific to this corpus (notes for writing manifests):
  - Skin malignant tumors 2025 (melanoma, squamous cell carcinoma, basal cell carcinoma, Merkel cell
    carcinoma, extramammary Paget's disease, cutaneous angiosarcoma, cutaneous lymphoma) come with
    **per-CQ "推奨決定資料" (recommendation decision materials)** as separate PDFs (`MM_CQ1_推奨決定資料.pdf` etc.)
  - The wound / pressure ulcer / burn guideline (2023) is in **6 volumes**; CQ numbers restart per volume
  - The urticaria guideline 2026 (4th ed.) is body + appendix tables + abridged version
  - Drug usage guidance, safe use manuals, proper use guides and clinical guides also go in the
    same section as society publications

usage:
    uv run python scripts/build_jda_guidelines.py            # build all manifests
    uv run python scripts/build_jda_guidelines.py --check    # validate only (no generation)
    uv run python scripts/build_jda_guidelines.py --only メラノーマ2025

profile / crop run the same scripts with `--corpus jda_guidelines`:
    uv run python scripts/profile_jns_pages.py --corpus jda_guidelines --render
    uv run --group ingest python scripts/crop_jns_figs.py --corpus jda_guidelines --merge
  (run from cage/)
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from build_jns_guidelines import main, use_corpus  # noqa: E402

if __name__ == "__main__":
    use_corpus("jda_guidelines")
    main(__doc__)
