"""cage API server entry point.

cage is the HTTP tool gateway called from LLENS. Currently only the doc domain
(knowledge base) is served; ocr and other domains will be added later.

Settings are read from **the repo root `.env`** (via python-dotenv). cage runs on the
host via `uv run cage`, so compose's env does not reach it; it reads the root `.env`
itself — write CAGE_* once at the root and both nucllei (compose) and cage get the
same values (writing the same CAGE_* values into 2 files was dropped 2026-09-07).
`cage/.env` is an optional override (to try different values in this dir only).
Precedence: process environment > cage/.env > root/.env.

Usage:
    cp .env.example .env  # once, at the repo root
    uv run cage           # listens on 0.0.0.0:13303

Calling uvicorn directly also works:
    uv run uvicorn cage.server:app --host 0.0.0.0 --port 13303
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

# bind defaults to 0.0.0.0 (effectively the only choice): the nucllei container reaches
# us via the docker bridge gateway (host.docker.internal). 127.0.0.1 would reject
# connections from the gateway IP. External exposure is limited by UFW to the Docker
# bridge subnet only (DEPLOYMENT.md).
DEFAULT_HOST = "0.0.0.0"
# port is in the llens internal range 133xx (the service table in DEPLOYMENT.md is the single source of truth).
DEFAULT_PORT = 13303

ROOT = Path(__file__).resolve().parent.parent      # cage/
REPO_ROOT = ROOT.parent                             # llens/


def main() -> None:
    # override=False, so whatever is read first wins: env vars > cage/.env (optional override) > root/.env (canonical)
    load_dotenv(ROOT / ".env", override=False)
    load_dotenv(REPO_ROOT / ".env", override=False)
    host = os.environ.get("CAGE_HOST", DEFAULT_HOST)
    port = int(os.environ.get("CAGE_PORT", str(DEFAULT_PORT)))
    log_level = os.environ.get("CAGE_LOG_LEVEL", "info").lower()

    # The app's own loggers show INFO and above. Used for request/response logging.
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    uvicorn.run("cage.server:app", host=host, port=port, log_level=log_level)


if __name__ == "__main__":
    main()
