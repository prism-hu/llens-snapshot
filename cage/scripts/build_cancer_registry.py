#!/usr/bin/env python3
"""Cancer registry manifests → doc tree `がん登録/` (thin wrapper around build_jns_guidelines).

Just switches the corpus to `cancer_registry` and calls the same main. **The manifest format,
the `patches:` / `pdfs[].url` specs and the options are all documented in the docstring of
`scripts/build_jns_guidelines.py`, the single source** (also shown by `--help`).

Where this corpus lives:
  - `scripts/cancer_registry/<id>.yaml`      … manifests (hand-written)
  - `cache/cancer_registry/pdf/<file>`       … original PDFs
  - `data/doc/がん登録/`                      … output (**a category directly under root**)
  - `data/static/cancer_registry/{docs,figs}/<slug>…` … delivered original PDFs and figures (flat)
  - `data/ingest/cancer_registry/`           … overlay, VLM batches, figs.json etc.

The front matter of `がん登録/_index.md` has **only title and the fields imposed on its children (<id>)**.
The root `data/doc/_index.yaml` has no `fields` = children carry title only (closed schema),
so writing guideline / publisher here makes the walker fail to start
(docs/cage/01_brief.md "front matter schema").

usage:
    uv run python scripts/build_cancer_registry.py            # build all manifests
    uv run python scripts/build_cancer_registry.py --check    # validate only (no generation)
    uv run python scripts/build_cancer_registry.py --only 標準登録様式2016

profile / crop run the same scripts with `--corpus cancer_registry`:
    uv run python scripts/profile_jns_pages.py --corpus cancer_registry --render
    uv run --group ingest python scripts/crop_jns_figs.py --corpus cancer_registry --merge
  (run from cage/)
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from build_jns_guidelines import main, use_corpus  # noqa: E402

if __name__ == "__main__":
    use_corpus("cancer_registry")
    main(__doc__)
