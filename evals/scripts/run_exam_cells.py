#!/usr/bin/env python3
"""Runner for the 2 national-exam tasks (igakuqa119 / jmle2026) in per-problem cells (for a remote OpenAI-compatible endpoint).

Imports build_messages / parse_answer / scoring / point allocation from
`evals/tasks/{igakuqa119,jmle2026}/run.py` as-is (= same prompt, extraction and scoring as the existing rows). Only the sending and saving differ:

- Streams to the gw (prism-gw -> litellm -> OpenRouter etc.) with an auth header
- Saves request.json / attempt-N.stream (raw SSE) / result.json per cell, and
  resumes by reusing completed cells. A cell left in `started` only (interrupted) is not silently recreated —
  only when `--archive-interrupted` is given explicitly is it moved to `_archived/` and rerun
- Conditions (cond):
  - `vision` … all 400 problems. Image problems get their image (a problem with has_image but no image file
    is sent text-only, same as the existing vision rows)
  - `blind`  … only image problems that have an image file. Same messages as vision minus the image block
- Retries only on transient transport errors (429 / 5xx / timeout / disconnect / cut off before [DONE] / error inside the stream).
  Never retries based on the score. finish_reason != stop and empty responses count as wrong
- Answer extraction uses content only (reasoning is not mixed in)
- The main run does not start unless the image probe (reading a string written only in the image) passes. No fallback to blind
- No new cell is started once cumulative usage.cost exceeds --budget-usd

Usage:
  python -m evals.scripts.run_exam_cells run --out <dir> --model openrouter/mimo-v2.6-pro \\
      --base-url http://<redacted-ip>:4000 --api-key-env NUCLLEI_LLM_API_KEY [--only jmle2026:120A-1 ...]
  python -m evals.scripts.run_exam_cells aggregate --out <dir> --agg-dir <model_selection/xxx>
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import datetime
import hashlib
import io
import json
import math
import random
import shutil
import statistics
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from evals.tasks.igakuqa119 import run as IG  # noqa: E402
from evals.tasks.jmle2026 import run as JM  # noqa: E402

MODS = {"igakuqa119": IG, "jmle2026": JM}
RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 529}
BACKOFF = [5, 20, 60, 120]


class Retryable(Exception):
    pass


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def save_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    tmp.replace(path)


def read_key(env_name: str) -> str:
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith(env_name + "="):
            return line.split("=", 1)[1].strip().strip("\"'")
    raise SystemExit(f"{env_name} not found in .env")


def text_only(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop image blocks only."""
    return [
        {**m, "content": [x for x in m["content"] if x["type"] == "text"]}
        if isinstance(m["content"], list) else m
        for m in messages
    ]


def qid_of(task: str, q: dict[str, Any]) -> str:
    return q["question_id"] if task == "jmle2026" else q["number"]


def has_image_of(task: str, q: dict[str, Any]) -> bool:
    return bool(q.get("requires_image" if task == "jmle2026" else "has_image", False))


def points_of(task: str, q: dict[str, Any]) -> int:
    return JM.points_for(q) if task == "jmle2026" else IG.points_for(q["number"], q["block"])


def score_of(task: str, q: dict[str, Any], content: str) -> dict[str, Any]:
    if task == "jmle2026":
        extracted, ok = JM.parse_answer(content, q["question_type"])
        correct = JM.is_correct(extracted, q["answer"], q["question_type"])
        return {"extracted": extracted, "extracted_set": extracted, "parse_ok": ok, "correct_raw": bool(correct)}
    extracted = IG.parse_answer(content)
    correct, ext_set = IG.score(q, extracted)
    ok = bool(IG.ANSWER_PATTERN.search(content))  # False = harness fell back to the whole text
    return {"extracted": extracted, "extracted_set": ext_set, "parse_ok": ok, "correct_raw": bool(correct)}


EXPECTED_MISSING_IMAGES = {"igakuqa119": {"119C15", "119C22"}, "jmle2026": set()}


def build_cells(model: str, sampling: dict[str, Any], extra: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cells: list[dict[str, Any]] = []
    info: dict[str, Any] = {"question_counts": {}, "pair_counts": {}, "excluded_from_pairs": [], "image_sha256": {}}
    for task, mod in MODS.items():
        qs = mod.load_problems()
        assert len(qs) == 400, (task, len(qs))
        missing = set()
        info["question_counts"][task] = len(qs)
        info["pair_counts"][task] = 0
        for q in qs:
            qid = qid_of(task, q)
            msgs, names = mod.build_messages(q, vision=True)
            n_img = sum(x["type"] == "image_url" for m in msgs if isinstance(m["content"], list) for x in m["content"])
            assert n_img == len(names), (task, qid)
            for n in names:
                info["image_sha256"][f"{task}/{n}"] = sha((mod.IMAGES_DIR / n).read_bytes())
            base = {"model": model, **sampling, **extra, "stream": True, "stream_options": {"include_usage": True}}
            cells.append({"task": task, "id": qid, "cond": "vision", "q": q, "image_files": names,
                          "body": {**base, "messages": msgs}})
            if has_image_of(task, q):
                if not names:
                    missing.add(qid)
                    info["excluded_from_pairs"].append({"task": task, "id": qid, "reason": "has_image but no image file in dataset (same exclusion as kimi-image-pairs-20260910)"})
                    continue
                info["pair_counts"][task] += 1
                cells.append({"task": task, "id": qid, "cond": "blind", "q": q, "image_files": [],
                              "body": {**base, "messages": text_only(msgs)}})
        assert missing == EXPECTED_MISSING_IMAGES[task], (task, sorted(missing))
    return cells, info


class Runner:
    def __init__(self, args: argparse.Namespace):
        self.a = args
        self.key = read_key(args.api_key_env)
        self.headers = {"Authorization": "Bearer " + self.key}
        self.url = args.base_url.rstrip("/") + "/v1/chat/completions"
        self.lock = threading.Lock()
        self.cost = 0.0
        self.stopped = False
        self.log = (args.out / "run.log").open("a")

    def logline(self, obj: dict[str, Any]) -> None:
        line = json.dumps({"t": datetime.datetime.now().astimezone().isoformat(timespec="seconds"), **obj}, ensure_ascii=False)
        with self.lock:
            self.log.write(line + "\n")
            self.log.flush()
        print(line, flush=True)

    def stream_once(self, body: dict[str, Any], trace: Path) -> dict[str, Any]:
        content = reasoning = reasoning_alt = ""
        usage: dict[str, Any] = {}
        finish = None
        done = False
        ttft = ttat = None
        start = time.monotonic()
        stream_error = None
        with trace.open("x") as f:
            try:
                with httpx.stream("POST", self.url, headers=self.headers, json=body,
                                  timeout=httpx.Timeout(self.a.timeout, connect=30)) as r:
                    if r.status_code != 200:
                        text = r.read().decode(errors="replace")[:1000]
                        f.write(f"HTTP {r.status_code}\n{text}\n")
                        if r.status_code in RETRY_STATUS:
                            raise Retryable(f"HTTP {r.status_code}: {text[:200]}")
                        raise RuntimeError(f"HTTP {r.status_code}: {text[:300]}")
                    for line in r.iter_lines():
                        if not line.startswith("data: "):
                            continue
                        payload = line[6:]
                        f.write(payload + "\n")
                        if payload == "[DONE]":
                            done = True
                            break
                        chunk = json.loads(payload)
                        if chunk.get("error"):
                            stream_error = json.dumps(chunk["error"], ensure_ascii=False)[:300]
                        usage = chunk.get("usage") or usage
                        for ch in chunk.get("choices") or []:
                            d = ch.get("delta") or {}
                            rc = d.get("reasoning_content") or ""
                            ra = d.get("reasoning") or ""
                            co = d.get("content") or ""
                            now = time.monotonic() - start
                            if (rc or ra or co) and ttft is None:
                                ttft = now
                            if co and ttat is None:
                                ttat = now
                            reasoning += rc
                            reasoning_alt += ra
                            content += co
                            finish = ch.get("finish_reason") or finish
            except (httpx.TimeoutException, httpx.TransportError) as e:
                raise Retryable(f"{type(e).__name__}: {e}") from e
        if stream_error:
            raise Retryable(f"stream error: {stream_error}")
        if not done:
            raise Retryable("stream ended before [DONE]")
        return {"content": content, "reasoning": reasoning or reasoning_alt,
                "reasoning_field": "reasoning_content" if reasoning else ("reasoning" if reasoning_alt else None),
                "usage": usage, "finish_reason": finish, "stream_done": done,
                "ttft_seconds": ttft, "ttat_seconds": ttat, "total_seconds": time.monotonic() - start}

    def cell_dir(self, c: dict[str, Any]) -> Path:
        return self.a.out / "cells" / c["task"] / f"{c['id']}.{c['cond']}"

    def run_cell(self, c: dict[str, Any]) -> dict[str, Any] | None:
        d = self.cell_dir(c)
        res = d / "result.json"
        if res.exists():
            return json.loads(res.read_text())
        with self.lock:
            if self.stopped or self.cost >= self.a.budget_usd:
                self.stopped = True
                return None
        assert not (d / "started.json").exists(), f"interrupted cell (use --archive-interrupted): {d}"
        d.mkdir(parents=True, exist_ok=True)
        body_bytes = json.dumps(c["body"], ensure_ascii=False, sort_keys=True).encode()
        req_sha = sha(body_bytes)
        (d / "request.json").write_bytes(body_bytes)
        save_json(d / "started.json", {"request_sha256": req_sha, "time": time.time()})
        q = c["q"]
        row: dict[str, Any] = {
            "task": c["task"], "id": c["id"], "cond": c["cond"], "block": q["block"],
            "has_image": has_image_of(c["task"], q), "image_files": c["image_files"],
            "points_possible": points_of(c["task"], q), "request_sha256": req_sha,
            "attempts": [], "error": None, "correct": False,
        }
        r = None
        for i in range(len(BACKOFF) + 1):
            t0 = time.time()
            try:
                r = self.stream_once(c["body"], d / f"attempt-{i + 1}.stream")
                row["attempts"].append({"ok": True, "seconds": round(time.time() - t0, 2)})
                break
            except Retryable as e:
                row["attempts"].append({"ok": False, "retryable": True, "error": str(e)[:300], "seconds": round(time.time() - t0, 2)})
                self.logline({"cell": d.name, "task": c["task"], "retry": i + 1, "error": str(e)[:200]})
                if i < len(BACKOFF):
                    time.sleep(BACKOFF[i])
            except Exception as e:  # non-retryable
                row["attempts"].append({"ok": False, "retryable": False, "error": f"{type(e).__name__}: {e}"[:300], "seconds": round(time.time() - t0, 2)})
                break
        if r is None:
            row["error"] = row["attempts"][-1]["error"]
        else:
            row.update(r)
            row["cost"] = float((r["usage"] or {}).get("cost") or 0.0)
            row["think_tag_in_content"] = "<think" in r["content"].lower()
            sc = score_of(c["task"], q, r["content"])
            row.update(sc)
            if r["finish_reason"] != "stop" or not r["content"].strip():
                row["error"] = f"incomplete (finish_reason={r['finish_reason']}, content_len={len(r['content'])})"
            row["correct"] = bool(sc["correct_raw"] and not row["error"])
        with self.lock:
            self.cost += row.get("cost", 0.0)
            total = self.cost
        save_json(res, row)
        self.logline({"cell": d.name, "task": c["task"], "correct": row["correct"], "error": row["error"],
                      "rt": (row.get("usage") or {}).get("reasoning_tokens"), "cost": round(row.get("cost", 0.0), 6),
                      "total_cost": round(total, 4)})
        return row


def make_probe_image() -> tuple[str, str]:
    from PIL import Image, ImageDraw, ImageFont
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 44)
    code, word = "4826", "HERON"
    im = Image.new("RGB", (460, 190), "white")
    dr = ImageDraw.Draw(im)
    dr.text((24, 24), f"CODE {code}", fill="black", font=font)
    dr.text((24, 104), f"WORD: {word}", fill="darkred", font=font)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode(), f"{code} {word}"


def cmd_run(a: argparse.Namespace) -> None:
    a.out.mkdir(parents=True, exist_ok=True)
    sampling = {"temperature": a.temperature, "top_p": a.top_p, "max_tokens": a.max_tokens}
    extra = json.loads(a.extra_body) if a.extra_body else {}
    cells, info = build_cells(a.model, sampling, extra)
    sources = {t: sha(Path(m.__file__).read_bytes()) for t, m in MODS.items()}
    datasets = {t: sha(json.dumps([c["q"] for c in cells if c["task"] == t and c["cond"] == "vision"],
                                  ensure_ascii=False, sort_keys=True).encode()) for t in MODS}
    manifest = {
        "scope": "igakuqa119 + jmle2026 full exam (vision) + blind re-run of image-bearing questions with image files",
        "model": a.model, "base_url": a.base_url, "sampling": sampling, "extra_body": extra,
        "thinking": "provider default (on); no chat_template_kwargs sent",
        "source_sha256": sources, "runner_sha256": sha(Path(__file__).read_bytes()),
        "question_sha256": datasets, **info,
        "policy": "Retry only transport errors (429/5xx/timeout/disconnect/no [DONE]/in-stream error). "
                  "No score-dependent retries. finish_reason!=stop or empty content counts as incorrect. "
                  "Answer extraction from content only. No blind fallback.",
    }
    mp = a.out / "manifest.json"
    if mp.exists():
        old = json.loads(mp.read_text())
        if old != manifest:
            diff = [k for k in set(old) | set(manifest) if old.get(k) != manifest.get(k)]
            raise SystemExit(f"run inputs changed vs manifest.json ({diff}); use a new --out")
    else:
        save_json(mp, manifest)

    if a.only:
        want = {tuple(x.split(":", 1)) for x in a.only}
        cells = [c for c in cells if (c["task"], c["id"]) in want]
    if a.conds:
        cells = [c for c in cells if c["cond"] in a.conds]
    if a.tasks:
        cells = [c for c in cells if c["task"] in a.tasks]

    runner = Runner(a)
    # interrupted cells
    interrupted = [runner.cell_dir(c) for c in cells
                   if (runner.cell_dir(c) / "started.json").exists() and not (runner.cell_dir(c) / "result.json").exists()]
    if interrupted:
        if not a.archive_interrupted:
            raise SystemExit("interrupted cells exist (rerun with --archive-interrupted to move them aside):\n"
                             + "\n".join(str(p) for p in interrupted))
        arch = a.out / "_archived" / datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        arch.mkdir(parents=True)
        for p in interrupted:
            shutil.move(str(p), arch / f"{p.parent.name}__{p.name}")
            runner.logline({"archived": str(p)})

    # cost already spent (all saved results in this out dir)
    runner.cost = sum(float(json.loads(p.read_text()).get("cost") or 0.0) for p in (a.out / "cells").rglob("result.json")) \
        if (a.out / "cells").exists() else 0.0
    runner.logline({"start": len(cells), "already_spent_usd": round(runner.cost, 4), "workers": a.workers})

    # image probe (once per out dir): text drawn only in the image must be read back
    probe_path = a.out / "probe.json"
    if not probe_path.exists() or not json.loads(probe_path.read_text()).get("passed"):
        url, expect = make_probe_image()
        body = {"model": a.model, **sampling, **extra, "max_tokens": 4096, "stream": True,
                "stream_options": {"include_usage": True},
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": "画像に書かれている4桁の数字と英単語をそのまま書き写してください。画像が見えない場合は「見えない」とだけ答えてください。"},
                    {"type": "image_url", "image_url": {"url": url}}]}]}
        trace = a.out / f"probe-{int(time.time())}.stream"
        r = runner.stream_once(body, trace)
        passed = all(tok in r["content"].upper() for tok in expect.split()) and r["finish_reason"] == "stop"
        save_json(probe_path, {"expect": expect, "content": r["content"], "usage": r["usage"],
                               "finish_reason": r["finish_reason"], "passed": passed, "trace": trace.name})
        runner.cost += float((r["usage"] or {}).get("cost") or 0.0)
        runner.logline({"probe": passed, "content": r["content"][:100]})
        if not passed:
            raise SystemExit("image probe failed; refusing to run (no blind fallback)")

    random.Random(20261013).shuffle(cells)
    with concurrent.futures.ThreadPoolExecutor(max_workers=a.workers) as pool:
        rows = list(pool.map(runner.run_cell, cells))
    done = [r for r in rows if r is not None]
    runner.logline({"finished": len(done), "skipped_budget": len(rows) - len(done),
                    "total_cost_usd": round(runner.cost, 4), "stopped_by_budget": runner.stopped})


# ---------------------------------------------------------------- aggregate

def load_rows(out: Path) -> list[dict[str, Any]]:
    return [json.loads(p.read_text()) for p in sorted((out / "cells").rglob("result.json"))]


def to_sample(task: str, q: dict[str, Any], r: dict[str, Any]):
    mod = MODS[task]
    common = dict(
        block=q["block"], is_required=q["block"] in mod.REQUIRED_BLOCKS, has_image=has_image_of(task, q),
        image_files=r["image_files"], points_possible=r["points_possible"], raw=r.get("content", ""),
        correct=r["correct"],
        ttft_ms=(r["ttft_seconds"] * 1000) if r.get("ttft_seconds") is not None else None,
        ttat_ms=(r["ttat_seconds"] * 1000) if r.get("ttat_seconds") is not None else None,
        total_time_ms=(r["total_seconds"] * 1000) if r.get("total_seconds") is not None else None,
        reasoning_tokens=int(((r.get("usage") or {}).get("completion_tokens_details") or {}).get("reasoning_tokens") or 0),
        answer_tokens=max(int((r.get("usage") or {}).get("completion_tokens") or 0)
                          - int(((r.get("usage") or {}).get("completion_tokens_details") or {}).get("reasoning_tokens") or 0), 0),
        finish_reason=r.get("finish_reason"),
    )
    if task == "jmle2026":
        return JM.SampleResult(question_id=r["id"], question_type=q["question_type"],
                               serial_group_id=(q.get("serial_group") or {}).get("group_id"),
                               gold=list(q["answer"]), extracted=r.get("extracted") or [],
                               parse_success=bool(r.get("parse_ok")), **common)
    return IG.SampleResult(problem_id=r["id"], is_numeric=not q["choices"], gold=q["gold"],
                           extracted=r.get("extracted") or "", extracted_set=r.get("extracted_set") or [], **common)


def exact_mcnemar(b: int, c: int) -> float:
    n = b + c
    if n == 0:
        return 1.0
    return min(1.0, 2 * sum(math.comb(n, k) for k in range(min(b, c) + 1)) / 2 ** n)


def cmd_aggregate(a: argparse.Namespace) -> None:
    manifest = json.loads((a.out / "manifest.json").read_text())
    rows = load_rows(a.out)
    by = {(r["task"], r["id"], r["cond"]): r for r in rows}
    report: dict[str, Any] = {"model": manifest["model"], "tasks": {}}
    a.agg_dir.mkdir(parents=True, exist_ok=True)
    for task, mod in MODS.items():
        qs = mod.load_problems()
        missing = [qid_of(task, q) for q in qs if (task, qid_of(task, q), "vision") not in by]
        if missing:
            print(f"[{task}] vision cells missing: {len(missing)}")
            report["tasks"][task] = {"complete": False, "missing_vision": len(missing)}
            continue
        samples = [to_sample(task, q, by[(task, qid_of(task, q), "vision")]) for q in qs]
        ts = [by[(task, qid_of(task, q), "vision")] for q in qs]
        start = datetime.datetime.fromisoformat(a.started) if a.started else datetime.datetime.now().astimezone()
        agg = mod.aggregate_results(manifest["model"], False, mod.ALL_BLOCKS, True, samples, start, datetime.datetime.now().astimezone())
        agg["run_dir"] = str(a.out)
        agg["cost_usd"] = sum(r.get("cost", 0.0) for r in ts)
        save_json(a.agg_dir / f"{task}.json", agg)

        # paired image analysis
        pairs = [qid_of(task, q) for q in qs if (task, qid_of(task, q), "blind") in by]
        vs = {q: by[(task, q, "vision")] for q in pairs}
        bs = {q: by[(task, q, "blind")] for q in pairs}
        wins = sorted(q for q in pairs if vs[q]["correct"] and not bs[q]["correct"])
        losses = sorted(q for q in pairs if bs[q]["correct"] and not vs[q]["correct"])
        diffs = [int(vs[q]["correct"]) - int(bs[q]["correct"]) for q in pairs]
        rng = random.Random(20260910)
        n = len(pairs)
        boots = sorted(100 * sum(rng.choices(diffs, k=n)) / n for _ in range(10000)) if n else [0] * 10000
        ptok = [(vs[q]["usage"]["prompt_tokens"], bs[q]["usage"]["prompt_tokens"]) for q in pairs
                if vs[q].get("usage") and bs[q].get("usage")]
        pair_res = {
            "n_pairs": n, "expected_pairs": manifest["pair_counts"][task],
            "vision_correct": sum(vs[q]["correct"] for q in pairs), "blind_correct": sum(bs[q]["correct"] for q in pairs),
            "vision_only": len(wins), "blind_only": len(losses), "vision_only_ids": wins, "blind_only_ids": losses,
            "difference_pp": 100 * (len(wins) - len(losses)) / n if n else 0.0,
            "paired_bootstrap_ci95_pp": [boots[249], boots[9749]],
            "exact_mcnemar_p": exact_mcnemar(len(wins), len(losses)),
            "prompt_tokens_vision_gt_blind": sum(v > b for v, b in ptok), "prompt_tokens_pairs": len(ptok),
            "prompt_tokens_median": {"vision": statistics.median(v for v, _ in ptok) if ptok else None,
                                     "blind": statistics.median(b for _, b in ptok) if ptok else None},
        }
        all_task_rows = [r for r in rows if r["task"] == task]
        u = lambda r, k: int((r.get("usage") or {}).get(k) or 0)  # noqa: E731
        rt = lambda r: int(((r.get("usage") or {}).get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)  # noqa: E731
        stats = {}
        for cond in ("vision", "blind"):
            rs = [r for r in all_task_rows if r["cond"] == cond]
            stats[cond] = {
                "cells": len(rs), "cost_usd": sum(r.get("cost", 0.0) for r in rs),
                "prompt_tokens": sum(u(r, "prompt_tokens") for r in rs),
                "completion_tokens": sum(u(r, "completion_tokens") for r in rs),
                "reasoning_tokens": sum(rt(r) for r in rs),
                "errors": sum(bool(r["error"]) for r in rs),
                "error_ids": sorted(f"{r['id']}: {r['error']}" for r in rs if r["error"]),
                "transport_retries": sum(len(r["attempts"]) - 1 for r in rs),
                "finish_reasons": JM._count([r.get("finish_reason") for r in rs]),
                "parse_fail": sorted(r["id"] for r in rs if not r.get("parse_ok")),
                "zero_reasoning": sorted(r["id"] for r in rs if rt(r) == 0),
                "think_tag_in_content": sorted(r["id"] for r in rs if r.get("think_tag_in_content")),
                "reasoning_field": JM._count([r.get("reasoning_field") for r in rs]),
            }
        report["tasks"][task] = {"complete": True, "leaderboard": agg["leaderboard"],
                                 "accuracy_by_block": agg["accuracy_by_block"], "pairs": pair_res, "stats": stats}
        print(f"[{task}] overall {agg['leaderboard']['overall']['score_str']} / {agg['leaderboard']['overall']['accuracy_str']}")
    save_json(a.out / "summary.json", report)
    print(json.dumps({t: {k: v for k, v in d.items() if k in ("pairs",)} for t, d in report["tasks"].items()}, ensure_ascii=False, indent=1))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--out", type=Path, required=True)
    r.add_argument("--model", required=True)
    r.add_argument("--base-url", required=True, help="without /v1")
    r.add_argument("--api-key-env", required=True, help="variable name in repo .env (value is never printed)")
    r.add_argument("--workers", type=int, default=6)
    r.add_argument("--temperature", type=float, default=0.0)
    r.add_argument("--top-p", type=float, default=1.0)
    r.add_argument("--max-tokens", type=int, default=32768)
    r.add_argument("--extra-body", default=None, help="JSON merged into every request body")
    r.add_argument("--timeout", type=float, default=900.0)
    r.add_argument("--budget-usd", type=float, default=10.0)
    r.add_argument("--only", nargs="*", help="task:id (pilot)")
    r.add_argument("--conds", nargs="*", choices=["vision", "blind"])
    r.add_argument("--tasks", nargs="*", choices=list(MODS))
    r.add_argument("--archive-interrupted", action="store_true")
    g = sub.add_parser("aggregate")
    g.add_argument("--out", type=Path, required=True)
    g.add_argument("--agg-dir", type=Path, required=True)
    g.add_argument("--started", default=None, help="ISO8601 start time to record")
    a = p.parse_args()
    {"run": cmd_run, "aggregate": cmd_aggregate}[a.cmd](a)


if __name__ == "__main__":
    main()
