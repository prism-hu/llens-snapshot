"""Runner for docqa (in-hospital document QA). SPEC.md is the contract.

Goes through nucllei's chat-wire API, so the doc tools / skills / sandbox run on the server side.
One chat per question. Results are **appended in completion order** to a jsonl with one line per
question (survives a crash midway). `--resume <results.jsonl>` skips finished ids and continues.

Usage:
  uv run --group evals python -m evals.tasks.docqa.run --model llens-thinking
  uv run --group evals python -m evals.tasks.docqa.run --model llens-no-tool --domain ctcae --limit 5
  uv run --group evals python -m evals.tasks.docqa.run --model llens-thinking \\
      --resume data/evals/docqa/llens-thinking/20260908-120000.jsonl

The endpoint is `EVAL_NUCLLEI_ORIGIN` / `EVAL_NUCLLEI_API_KEY` (OS environment variables -> `.env` at the repo root).
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx
from tqdm import tqdm

from evals.harness.nucllei_client import AskResult, NucllieClient

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]
QUESTIONS_DIR = Path(__file__).resolve().parent / "questions"
RESULTS_DIR = REPO_ROOT / "data" / "evals" / "docqa"  # data = symlink to ~/Sync/Projects/llens/data (outside git)

# Part of the protocol. Appended to the question as one line (grade.py looks at the 【回答】/【根拠】 lines).
PROMPT_SUFFIX = "最後に【回答】の行に短い答えだけを書き、根拠にした文書のパスがあれば【根拠】の行に書いてください。"

ENV_ORIGIN = "EVAL_NUCLLEI_ORIGIN"
ENV_API_KEY = "EVAL_NUCLLEI_API_KEY"


# -- env -----------------------------------------------------------------------


def load_env(path: Path) -> dict[str, str]:
    """Simple .env parser (same rules as load_env in scripts/nucllei/sync.py).
    KEY=VAL lines only, quotes stripped, comments ignored. OS environment variables take precedence."""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def resolve_connection() -> tuple[str, str]:
    dotenv = load_env(REPO_ROOT / ".env")
    origin = os.environ.get(ENV_ORIGIN) or dotenv.get(ENV_ORIGIN, "")
    api_key = os.environ.get(ENV_API_KEY) or dotenv.get(ENV_API_KEY, "")
    if not origin or not api_key:
        raise SystemExit(f"{ENV_ORIGIN} / {ENV_API_KEY} missing (OS env or {REPO_ROOT / '.env'})")
    return origin, api_key


# -- questions -----------------------------------------------------------------


def load_questions(domains: list[str] | None = None) -> list[dict[str, Any]]:
    """Read all questions/*.jsonl. File name order -> line order."""
    out: list[dict[str, Any]] = []
    for f in sorted(QUESTIONS_DIR.glob("*.jsonl")):
        if domains and f.stem not in domains:
            continue
        for lineno, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                q = json.loads(line)
            except json.JSONDecodeError as e:
                raise SystemExit(f"{f}:{lineno}: broken JSON: {e}") from e
            q.setdefault("domain", f.stem)
            out.append(q)
    if domains:
        out = [q for q in out if q.get("domain") in domains]
    return out


def build_prompt(question: str) -> str:
    return f"{question.rstrip()}\n\n{PROMPT_SUFFIX}"


def load_done_ids(path: Path) -> set[str]:
    ids: set[str] = set()
    if not path.exists():
        return ids
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("id"):
            ids.add(row["id"])
    return ids


# -- result row ----------------------------------------------------------------


@dataclass
class ResultRow:
    # question fields (in SPEC.md order)
    id: str
    domain: str
    qtype: str
    question: str
    answer: str
    answer_kind: str
    accept: list[str]
    gold_paths: list[str]
    gold_pages: list[Any]
    needs_sandbox: bool
    rationale: str
    author_check: bool
    # run fields
    model: str
    chat_id: str | None
    content: str
    reasoning_chars: int
    tool_calls: list[dict[str, Any]]
    n_tool_calls: int
    usage: dict[str, Any]
    ttft_ms: float | None
    ttat_ms: float | None
    total_ms: float | None
    error: str | None
    timestamp: str
    extra: dict[str, Any] = field(default_factory=dict)  # keep any extra keys the question has
    # full text (since 2026-09-09). Rows before that do not have it (only reasoning_chars)
    reasoning: str = ""  # full text of the reasoning block
    content_blocks: list[dict[str, Any]] = field(default_factory=list)  # content_blocks of the persisted message (includes tool arguments and results)


QUESTION_KEYS = (
    "id", "domain", "qtype", "question", "answer", "answer_kind", "accept",
    "gold_paths", "gold_pages", "needs_sandbox", "rationale", "author_check",
)


def make_row(q: dict[str, Any], model: str, res: AskResult | None, error: str | None) -> ResultRow:
    now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    extra = {k: v for k, v in q.items() if k not in QUESTION_KEYS}
    base = dict(
        id=q["id"],
        domain=q.get("domain", ""),
        qtype=q.get("qtype", ""),
        question=q["question"],
        answer=q.get("answer", ""),
        answer_kind=q.get("answer_kind", ""),
        accept=list(q.get("accept") or []),
        gold_paths=list(q.get("gold_paths") or []),
        gold_pages=list(q.get("gold_pages") or []),
        needs_sandbox=bool(q.get("needs_sandbox", False)),
        rationale=q.get("rationale", ""),
        author_check=bool(q.get("author_check", False)),
        model=model,
        timestamp=now,
        extra=extra,
    )
    if res is None:
        return ResultRow(
            **base, chat_id=None, content="", reasoning_chars=0, tool_calls=[], n_tool_calls=0,
            usage={}, ttft_ms=None, ttat_ms=None, total_ms=None, error=error,
        )
    tool_calls = [{"name": tc.get("name"), "arguments": tc.get("arguments")} for tc in res.tool_calls]
    return ResultRow(
        **base,
        chat_id=res.chat_id,
        content=res.content,
        reasoning_chars=len(res.reasoning or ""),
        tool_calls=tool_calls,
        n_tool_calls=len(tool_calls),
        usage=res.usage or {},
        ttft_ms=res.ttft_ms,
        ttat_ms=res.ttat_ms,
        total_ms=res.total_ms,
        error=error if error is not None else res.error,
        reasoning=res.reasoning or "",
        content_blocks=list((res.raw or {}).get("content_blocks") or []),
    )


# -- execution -----------------------------------------------------------------


def ask_with_retry(client: NucllieClient, model: str, q: dict[str, Any]) -> tuple[AskResult | None, str | None]:
    """Transport errors (disconnect / timeout) are retried once. HTTP 4xx/5xx are errors as-is."""
    prompt = build_prompt(q["question"])
    title = f"docqa {q['id']}"
    last: Exception | None = None
    for attempt in range(2):
        try:
            return client.ask(model, prompt, title=title), None
        except httpx.TransportError as e:
            last = e
            log.warning("%s: transport error (attempt %d/2): %s", q["id"], attempt + 1, e)
        except httpx.HTTPStatusError as e:
            return None, f"http {e.response.status_code}: {e}"
        except Exception as e:  # noqa: BLE001 — one failed question must not bring down the whole run
            return None, f"{type(e).__name__}: {e}"
    return None, f"transport error after retry: {last}"


def run(
    *,
    model: str,
    questions: list[dict[str, Any]],
    out_path: Path,
    concurrency: int,
    delete_chats: bool,
) -> tuple[int, int]:
    origin, api_key = resolve_connection()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    write_lock = threading.Lock()
    n_err = 0

    def work(q: dict[str, Any]) -> ResultRow:
        res, err = ask_with_retry(client, model, q)
        row = make_row(q, model, res, err)
        if delete_chats and res is not None:
            try:
                client.delete_chat(res.chat_id)
            except httpx.HTTPError as e:
                log.warning("%s: delete chat %s failed: %s", q["id"], res.chat_id, e)
        return row

    with NucllieClient(origin, api_key) as client, out_path.open("a", encoding="utf-8") as fh:
        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
            futures = {pool.submit(work, q): q for q in questions}
            pbar = tqdm(total=len(futures), desc=f"docqa {model}", unit="q")
            try:
                for fut in as_completed(futures):
                    row = fut.result()
                    if row.error:
                        n_err += 1
                    with write_lock:
                        fh.write(json.dumps(asdict(row), ensure_ascii=False) + "\n")
                        fh.flush()
                    pbar.update(1)
                    pbar.set_postfix(err=n_err, tools=row.n_tool_calls)
            except KeyboardInterrupt:
                print("\n[interrupt] pending questions cancelled (waiting for the running ones to finish)", file=sys.stderr)
                pool.shutdown(wait=True, cancel_futures=True)
                raise
            finally:
                pbar.close()
    return len(questions), n_err


def default_out(model: str) -> Path:
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    return RESULTS_DIR / model / f"{stamp}.jsonl"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", required=True, help="nucllei model id (llens-thinking / llens-no-tool etc.)")
    parser.add_argument("--domain", action="append", default=None,
                        help="filter by domain (questions/<domain>.jsonl). Repeatable")
    parser.add_argument("--limit", type=int, default=None, help="only the first N questions")
    parser.add_argument("--resume", type=Path, default=None,
                        help="existing results.jsonl. Skips the ids it contains (appends to this file when --out is omitted)")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--delete-chats", action="store_true", help="delete the server-side chats after collection")
    parser.add_argument("--out", type=Path, default=None,
                        help="output jsonl (default data/evals/docqa/<model>/<yyyymmdd-HHMMSS>.jsonl)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")

    questions = load_questions(args.domain)
    if not questions:
        print(f"[docqa] no questions: {QUESTIONS_DIR} (domain={args.domain})", file=sys.stderr)
        return 1
    if args.limit:
        questions = questions[: args.limit]

    out_path = args.out or args.resume or default_out(args.model)
    if args.resume:
        done = load_done_ids(args.resume)
        before = len(questions)
        questions = [q for q in questions if q["id"] not in done]
        print(f"[resume] {before - len(questions)} questions already done -> {len(questions)} remaining")
        if not questions:
            print(f"[done] docqa -> {out_path}")
            return 0

    print(f"[docqa] model={args.model} n={len(questions)} concurrency={args.concurrency} -> {out_path}")
    n, n_err = run(
        model=args.model,
        questions=questions,
        out_path=out_path,
        concurrency=args.concurrency,
        delete_chats=args.delete_chats,
    )
    print(f"[done] docqa {n} questions (error {n_err}) -> {out_path}")
    print(f"       uv run --group evals python -m evals.tasks.docqa.grade {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
