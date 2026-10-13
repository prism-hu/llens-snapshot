"""Save the whole server-side chat for each chat_id in results.jsonl (the complete record of docqa).

A results row holds only the answer body and tool arguments (before 2026-09-09), or is summary-oriented even when it has more,
so the **persisted chat itself**, including the full reasoning and full tool results, is dumped to `<results dir>/chats/<question id>.json`.
With `--executions`, `GET /api/v1/chats/{id}/executions` (background executions) is also saved to
`<question id>.executions.json`.

Usage:
  uv run --group evals python -m evals.tasks.docqa.dump_chats data/evals/docqa/llens-thinking/20260908-all.jsonl
  uv run --group evals python -m evals.tasks.docqa.dump_chats a.jsonl b.jsonl --out DIR --executions --force

The endpoint is the same as run.py: `EVAL_NUCLLEI_ORIGIN` / `EVAL_NUCLLEI_API_KEY` (OS env -> `.env` at the repo root).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from evals.harness.nucllei_client import NucllieClient
from evals.tasks.docqa.run import resolve_connection

log = logging.getLogger(__name__)


@dataclass
class DumpStats:
    rows: int = 0
    no_chat_id: int = 0
    skipped: int = 0
    written: int = 0
    executions_written: int = 0
    failed: int = 0
    bytes: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)


def load_rows(paths: list[Path]) -> list[dict[str, Any]]:
    """Read several results.jsonl. If the same id appears more than once the last wins (all.jsonl is run1+run2 concatenated, so normally no duplicates)."""
    by_id: dict[str, dict[str, Any]] = {}
    for p in paths:
        for lineno, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as e:
                log.warning("%s:%d: broken JSON, skipping: %s", p, lineno, e)
                continue
            key = row.get("id") or row.get("chat_id")
            if not key:
                continue
            by_id[key] = row
    return list(by_id.values())


def dump_chats(
    client: NucllieClient,
    rows: list[dict[str, Any]],
    out_dir: Path,
    *,
    force: bool = False,
    executions: bool = False,
) -> DumpStats:
    out_dir.mkdir(parents=True, exist_ok=True)
    st = DumpStats()
    for row in rows:
        st.rows += 1
        chat_id = row.get("chat_id")
        if not chat_id:
            st.no_chat_id += 1
            continue
        qid = row.get("id") or chat_id
        chat_path = out_dir / f"{qid}.json"
        exec_path = out_dir / f"{qid}.executions.json"
        need_chat = force or not chat_path.exists()
        need_exec = executions and (force or not exec_path.exists())
        if not need_chat and not need_exec:
            st.skipped += 1
            continue
        try:
            if need_chat:
                chat = client.get_chat(chat_id)
                data = json.dumps(chat, ensure_ascii=False, indent=1)
                chat_path.write_text(data, encoding="utf-8")
                st.written += 1
                st.bytes += len(data.encode("utf-8"))
            if need_exec:
                ex = client.get_executions(chat_id)
                data = json.dumps(ex, ensure_ascii=False, indent=1)
                exec_path.write_text(data, encoding="utf-8")
                st.executions_written += 1
                st.bytes += len(data.encode("utf-8"))
        except httpx.HTTPError as e:
            st.failed += 1
            st.failures.append((qid, f"{type(e).__name__}: {e}"))
            log.warning("%s (chat %s): %s", qid, chat_id, e)
    return st


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("results", type=Path, nargs="+", help="results.jsonl (multiple allowed)")
    parser.add_argument("--out", type=Path, default=None,
                        help="output dir (default: chats/ in the same dir as the first results)")
    parser.add_argument("--force", action="store_true", help="re-fetch even if already saved")
    parser.add_argument("--executions", action="store_true", help="also save executions")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")

    for p in args.results:
        if not p.exists():
            print(f"[dump_chats] not found: {p}", file=sys.stderr)
            return 1
    out_dir = args.out or args.results[0].resolve().parent / "chats"
    rows = load_rows(args.results)
    origin, api_key = resolve_connection()
    with NucllieClient(origin, api_key) as client:
        st = dump_chats(client, rows, out_dir, force=args.force, executions=args.executions)

    print(f"[dump_chats] rows={st.rows} no_chat_id={st.no_chat_id} skipped={st.skipped} "
          f"written={st.written} executions={st.executions_written} failed={st.failed} "
          f"bytes={st.bytes} -> {out_dir}")
    for qid, err in st.failures:
        print(f"  failed {qid}: {err}", file=sys.stderr)
    return 1 if st.failed else 0


if __name__ == "__main__":
    sys.exit(main())
