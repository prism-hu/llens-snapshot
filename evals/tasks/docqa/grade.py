"""Grader for docqa results (SPEC.md "Running and grading").

Usage:
  uv run --group evals python -m evals.tasks.docqa.grade results.jsonl
  uv run --group evals python -m evals.tasks.docqa.grade a.jsonl --compare b.jsonl
  uv run --group evals python -m evals.tasks.docqa.grade results.jsonl --md          # also write <results>.grade.md
  uv run --group evals python -m evals.tasks.docqa.grade results.jsonl --graded g.jsonl  # per-question grading, one line each
  uv run --group evals python -m evals.tasks.docqa.grade results.jsonl --questions DIR   # use a different question dir

Where the question fields come from:
- accept / answer_kind / gold_paths / qtype / domain used for grading are looked up by id in the **current questions/*.jsonl**
  (default: `questions/` next to this file, changed with `--questions DIR`). The snapshot stored in results.jsonl
  is only a fallback when the id is not in questions (warning on stderr). Fixes to the questions apply to grading without a rerun.

Grading rules:
- answer: the block from the last 【回答】 marker in the body to the next 【根拠】 marker (or the end if none)
  (multi-line allowed; the whole body if empty). Half-width [回答] and the typo 【根根拠】 are accepted as markers too.
  NFKC -> strip commas and whitespace -> lowercase.
- number: any numeric token in the answer block (integer / decimal, negative allowed) is numerically equal to any of accept
  (1.5050 == 1.505、291 != 291.5、29 != 291)
- term / bool: any of accept is a substring (case-insensitive)
- list: **all** of accept are substrings
- absent: a fixed phrase set (ABSENT_PATTERN) appears in the answer line. citation equals correct
- citation correct (other than absent): any of gold_paths appears in the body, or in a read_doc path argument.
  Empty gold_paths (filter-type questions etc. with nothing to cite) is N/A (None) and not counted in the cit_acc denominator (n_cit)
- --compare: pair by common id, per-question bootstrap 95%CI of the accuracy difference (10,000 resamples, fixed seed) and
  McNemar exact p (binomtest if scipy is available, otherwise computed by hand). N/A rows are dropped from both sides
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import unicodedata
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

# Both full-width 【】 and half-width [] markers are accepted. 【根根拠】 is a frequent model typo.
ANSWER_MARKER = re.compile(r"[【\[]\s*回答\s*[】\]]\s*[:：]?")
CITATION_MARKER = re.compile(r"[【\[]\s*根+拠\s*[】\]]\s*[:：]?")
MARKDOWN_NOISE = re.compile(r"[*_`#]+")

# The fixed phrase set from SPEC plus forms observed in correct refusals in the live run (2026-09-08, 120 questions). Observed forms:
#   未収録 / 収録されていない / 収録されていません / 収録されておりません / 収録されておらず /
#   収録されていないため / 収録がない / 含まれていない / 含まれておらず / 確認できない / 確認できず /
#   見当たらない / 見当たりません / 存在しない / 存在せず / 掲載がない / 掲載されていません /
#   該当する〜ない / 該当〜ありません / 回答できません / 引くことができません
# Matched against the normalized (whitespace-stripped) answer block.
ABSENT_PATTERN = re.compile(
    "|".join([
        r"未収録",
        r"収録されて(い|お)らず",
        r"収録され(てい|ており)?(ない|ません)",
        r"収録(が|は)?(ない|ありません|無い)",
        r"含まれて(い|お)らず",
        r"含まれ(てい|ており)?(ない|ません)",
        r"確認でき(ず|ない|ません)",
        r"見当たら(ず|ない)", r"見当たりません",
        r"存在(しない|しません|せず)",
        r"掲載(が|は)?(ない|されて(い|お)らず|され(てい|ており)?(ない|ません))",
        r"該当(する)?.{0,8}(ない|ありません)",
        r"回答できません",
        r"引くことができません",
        r"ありません",
    ])
)

SANDBOX_TOOLS = {"run_python", "run_python_once", "run_shell"}
READ_DOC_TOOLS = {"read_doc"}

QUESTIONS_DIR = Path(__file__).resolve().parent / "questions"
QUESTION_FIELDS = ("accept", "answer_kind", "gold_paths", "qtype", "domain")

BOOTSTRAP_N = 10_000
BOOTSTRAP_SEED = 20260908


# -- normalization / matching --------------------------------------------------


def normalize(s: str) -> str:
    """NFKC (full-width -> half-width) -> strip commas and whitespace -> lowercase."""
    s = unicodedata.normalize("NFKC", s or "")
    s = s.replace(",", "").replace("、", "")
    s = re.sub(r"\s+", "", s)
    return s.lower()


def extract_answer(content: str) -> tuple[str, bool]:
    """The block from the last 【回答】 marker to the next 【根拠】 marker (or the end if none).

    Multi-line allowed. Text written on the marker line itself (【回答】500/mm³) is part of the block.
    Falls back to the whole body if the block is empty. Returns (text, found).
    """
    content = content or ""
    markers = list(ANSWER_MARKER.finditer(content))
    if markers:
        start = markers[-1].end()
        cit = CITATION_MARKER.search(content, start)
        block = content[start:cit.start()] if cit else content[start:]
        text = MARKDOWN_NOISE.sub("", block).strip()
        if text:
            return text, True
    return content.strip(), False


def extract_citation_line(content: str) -> str:
    """The first line after the last 【根拠】 marker."""
    content = content or ""
    markers = list(CITATION_MARKER.finditer(content))
    if not markers:
        return ""
    rest = content[markers[-1].end():].lstrip()
    return MARKDOWN_NOISE.sub("", rest.split("\n", 1)[0]).strip()


NUMBER_TOKEN = re.compile(r"-?\d+(?:\.\d+)?")


def _to_decimal(s: str) -> Decimal | None:
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def number_matches(answer_norm: str, accept: str) -> bool:
    """Any numeric token in the answer is numerically equal to accept (1.5050 == 1.505, 291 != 291.5)."""
    acc = _to_decimal(normalize(accept))
    if acc is None:
        return False
    for tok in NUMBER_TOKEN.findall(answer_norm):
        v = _to_decimal(tok)
        if v is not None and v == acc:
            return True
    return False


def term_matches(answer_norm: str, accept: str) -> bool:
    acc = normalize(accept)
    return bool(acc) and acc in answer_norm


def is_correct(answer_kind: str, accept: list[str], answer_text: str) -> bool:
    ans = normalize(answer_text)
    if answer_kind == "number":
        return any(number_matches(ans, a) for a in accept)
    if answer_kind in ("term", "bool"):
        return any(term_matches(ans, a) for a in accept)
    if answer_kind == "list":
        return bool(accept) and all(term_matches(ans, a) for a in accept)
    if answer_kind == "absent":
        return ABSENT_PATTERN.search(ans) is not None
    raise ValueError(f"unknown answer_kind: {answer_kind!r}")


def _string_values(obj: Any) -> list[str]:
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [s for v in obj.values() for s in _string_values(v)]
    if isinstance(obj, (list, tuple)):
        return [s for v in obj for s in _string_values(v)]
    return []


def read_doc_paths(tool_calls: list[dict[str, Any]]) -> list[str]:
    """path arguments (string / array) of read_doc calls."""
    out: list[str] = []
    for tc in tool_calls or []:
        if tc.get("name") not in READ_DOC_TOOLS:
            continue
        args = tc.get("arguments")
        if isinstance(args, dict):
            for key in ("path", "paths"):
                out.extend(_string_values(args.get(key)))
        elif isinstance(args, str):
            out.append(args)
    return out


def _path_key(p: str) -> str:
    p = unicodedata.normalize("NFKC", p or "").strip()
    p = re.sub(r"\.md$", "", p)
    return p.strip("/")


def is_citation_correct(gold_paths: list[str], content: str, tool_calls: list[dict[str, Any]]) -> bool:
    golds = [_path_key(g) for g in gold_paths if g]
    if not golds:
        return False
    body = unicodedata.normalize("NFKC", content or "")
    if any(g in body for g in golds):
        return True
    for p in read_doc_paths(tool_calls):
        pk = _path_key(p)
        if any(g == pk or g in pk for g in golds):
            return True
    return False


# -- per-row grading -----------------------------------------------------------


def load_questions(qdir: Path = QUESTIONS_DIR) -> dict[str, dict[str, Any]]:
    """Read questions/*.jsonl as id -> question dict. The last one wins for a duplicate id."""
    out: dict[str, dict[str, Any]] = {}
    for path in sorted(Path(qdir).glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                q = json.loads(line)
                out[q["id"]] = q
    return out


def grade_row(row: dict[str, Any], questions: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Grade one row. When questions is given, the question fields are overridden by its current version.

    When the id is not in questions, the snapshot on the results side is used and a warning goes to stderr.
    With questions=None (tests etc.) the row's fields are used as-is.
    """
    if questions is not None:
        q = questions.get(row.get("id"))
        if q is None:
            print(f"[grade] warning: id {row.get('id')!r} not in questions dir; using snapshot in results",
                  file=sys.stderr)
        else:
            row = {**row, **{k: q[k] for k in QUESTION_FIELDS if k in q}}
    content = row.get("content") or ""
    tool_calls = row.get("tool_calls") or []
    answer_text, found = extract_answer(content)
    kind = row.get("answer_kind", "term")
    correct = False
    if not row.get("error") or content:
        correct = is_correct(kind, list(row.get("accept") or []), answer_text)
    gold_paths = [g for g in (row.get("gold_paths") or []) if g]
    citation: bool | None
    if kind == "absent":
        citation = correct
    elif not gold_paths:
        citation = None  # questions with nothing to cite (filter-type sandbox aggregation etc.) are N/A
    else:
        citation = is_citation_correct(gold_paths, content, tool_calls)
    names = [tc.get("name") for tc in tool_calls]
    n_tools = row.get("n_tool_calls")
    if n_tools is None:
        n_tools = len(tool_calls)
    return {
        "id": row.get("id"),
        "domain": row.get("domain", ""),
        "qtype": row.get("qtype", ""),
        "answer_kind": kind,
        "needs_sandbox": bool(row.get("needs_sandbox", False)),
        "extracted": answer_text[:200],
        "answer_line_found": found,
        "correct": correct,
        "citation_correct": citation,
        "tool_used": n_tools > 0,
        "used_sandbox": any(n in SANDBOX_TOOLS for n in names),
        "n_tool_calls": n_tools,
        "total_ms": row.get("total_ms"),
        "error": row.get("error"),
    }


def load_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    # if the same id appears more than once (duplicates from resume etc.) take the last one
    by_id: dict[str, dict[str, Any]] = {}
    for r in rows:
        by_id[r.get("id")] = r
    return list(by_id.values())


# -- aggregation ---------------------------------------------------------------


def summarize(graded: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(graded)
    if n == 0:
        return {"n": 0, "acc": None, "n_cit": 0, "cit_acc": None, "mean_tools": None, "median_ms": None,
                "tool_used": None, "sandbox": None, "errors": 0}
    ms = [g["total_ms"] for g in graded if g.get("total_ms") is not None]
    cits = [g["citation_correct"] for g in graded if g["citation_correct"] is not None]
    return {
        "n": n,
        "acc": sum(g["correct"] for g in graded) / n,
        "n_cit": len(cits),
        "cit_acc": sum(cits) / len(cits) if cits else None,
        "mean_tools": sum(g["n_tool_calls"] for g in graded) / n,
        "median_ms": statistics.median(ms) if ms else None,
        "tool_used": sum(g["tool_used"] for g in graded) / n,
        "sandbox": sum(g["used_sandbox"] for g in graded) / n,
        "errors": sum(1 for g in graded if g.get("error")),
    }


def _fmt_pct(x: float | None) -> str:
    return "-" if x is None else f"{100 * x:.1f}%"


def _fmt_num(x: float | None, d: int = 1) -> str:
    return "-" if x is None else f"{x:.{d}f}"


def _table(rows: list[tuple[str, dict[str, Any]]]) -> list[str]:
    cols = ["group", "n", "acc", "n_cit", "cit_acc", "tool_used", "sandbox", "mean_tools", "median_ms", "err"]
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join(["---"] + ["---:"] * (len(cols) - 1)) + "|"]
    for name, s in rows:
        lines.append("| " + " | ".join([
            name, str(s["n"]), _fmt_pct(s["acc"]), str(s["n_cit"]), _fmt_pct(s["cit_acc"]),
            _fmt_pct(s["tool_used"]), _fmt_pct(s["sandbox"]), _fmt_num(s["mean_tools"]),
            _fmt_num(s["median_ms"], 0), str(s["errors"]),
        ]) + " |")
    return lines


def _groups(graded: list[dict[str, Any]], key: str) -> list[tuple[str, dict[str, Any]]]:
    keys = sorted({g[key] for g in graded})
    return [(k, summarize([g for g in graded if g[key] == k])) for k in keys]


def render_report(label: str, graded: list[dict[str, Any]]) -> str:
    out = [f"## docqa: {label}", ""]
    out += _table([("overall", summarize(graded))])
    out += ["", "### by domain", ""]
    out += _table(_groups(graded, "domain"))
    out += ["", "### by qtype", ""]
    out += _table(_groups(graded, "qtype"))
    out += ["", "### domain × qtype (acc / cit_acc; cit_acc only over rows with n_cit > 0)", ""]
    domains = sorted({g["domain"] for g in graded})
    qtypes = sorted({g["qtype"] for g in graded})
    out.append("| domain | " + " | ".join(qtypes) + " |")
    out.append("|---|" + "|".join(["---:"] * len(qtypes)) + "|")
    for d in domains:
        cells = []
        for t in qtypes:
            s = summarize([g for g in graded if g["domain"] == d and g["qtype"] == t])
            if s["n"] == 0:
                cells.append("-")
            elif s["n_cit"] == s["n"]:
                cells.append(f"{_fmt_pct(s['acc'])} / {_fmt_pct(s['cit_acc'])} (n={s['n']})")
            else:
                cells.append(f"{_fmt_pct(s['acc'])} / {_fmt_pct(s['cit_acc'])} (n={s['n']}, n_cit={s['n_cit']})")
        out.append(f"| {d} | " + " | ".join(cells) + " |")
    return "\n".join(out) + "\n"


# -- paired comparison ---------------------------------------------------------


def bootstrap_ci(diffs: list[float], n_resamples: int = BOOTSTRAP_N, seed: int = BOOTSTRAP_SEED) -> tuple[float, float]:
    """Resample the per-question difference vector with replacement for the 95% CI of the mean difference."""
    import numpy as np

    arr = np.asarray(diffs, dtype=float)
    if arr.size == 0:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, arr.size, size=(n_resamples, arr.size))
    means = arr[idx].mean(axis=1)
    lo, hi = np.percentile(means, [2.5, 97.5])
    return (float(lo), float(hi))


def _binom_two_sided_p(k: int, n: int) -> float:
    """Binomial test with p=0.5 (two-sided, for when scipy is missing)."""
    from math import comb

    if n == 0:
        return 1.0
    pmf = [comb(n, i) / 2 ** n for i in range(n + 1)]
    pk = pmf[k]
    return min(1.0, sum(p for p in pmf if p <= pk * (1 + 1e-12)))


def mcnemar_exact_p(b: int, c: int) -> float:
    """b = A correct / B wrong, c = A wrong / B correct. Binomial test with p=0.5 over discordant pairs only."""
    n = b + c
    if n == 0:
        return 1.0
    try:
        from scipy.stats import binomtest

        return float(binomtest(min(b, c), n, 0.5, alternative="two-sided").pvalue)
    except ImportError:
        return _binom_two_sided_p(min(b, c), n)


def compare(a: list[dict[str, Any]], b: list[dict[str, Any]], metric: str) -> dict[str, Any]:
    """Pair by common id. Rows whose metric is None (N/A) on either side are dropped."""
    am = {g["id"]: g for g in a}
    bm = {g["id"]: g for g in b}
    ids = sorted(i for i in set(am) & set(bm) if am[i][metric] is not None and bm[i][metric] is not None)
    xa = [int(am[i][metric]) for i in ids]
    xb = [int(bm[i][metric]) for i in ids]
    diffs = [y - x for x, y in zip(xa, xb)]
    b_only = sum(1 for x, y in zip(xa, xb) if x and not y)  # A correct, B wrong
    c_only = sum(1 for x, y in zip(xa, xb) if y and not x)  # A wrong, B correct
    n = len(ids)
    lo, hi = bootstrap_ci(diffs) if n else (float("nan"), float("nan"))
    return {
        "n": n,
        "a": sum(xa) / n if n else None,
        "b": sum(xb) / n if n else None,
        "diff": sum(diffs) / n if n else None,
        "ci": (lo, hi),
        "a_only": b_only,
        "b_only": c_only,
        "p": mcnemar_exact_p(b_only, c_only),
    }


def render_compare(label_a: str, label_b: str, a: list[dict[str, Any]], b: list[dict[str, Any]]) -> str:
    out = [f"## compare: {label_b} − {label_a} (paired, n = common ids; citation_correct only over rows that are not N/A on either side)", ""]
    cols = ["metric", "n", label_a, label_b, "Δ (B−A)", "95% CI (bootstrap)", "A✓B✗ / A✗B✓", "McNemar p"]
    out.append("| " + " | ".join(cols) + " |")
    out.append("|---|" + "|".join(["---:"] * (len(cols) - 1)) + "|")
    for metric in ("correct", "citation_correct", "tool_used"):
        r = compare(a, b, metric)
        ci = "-" if r["n"] == 0 else f"[{100 * r['ci'][0]:+.1f}, {100 * r['ci'][1]:+.1f}] pt"
        out.append("| " + " | ".join([
            metric, str(r["n"]), _fmt_pct(r["a"]), _fmt_pct(r["b"]),
            "-" if r["diff"] is None else f"{100 * r['diff']:+.1f} pt",
            ci, f"{r['a_only']} / {r['b_only']}", f"{r['p']:.4f}",
        ]) + " |")
    out += ["", "### Δacc by domain", ""]
    domains = sorted({g["domain"] for g in a} | {g["domain"] for g in b})
    out.append("| domain | n | " + label_a + " | " + label_b + " | Δ | 95% CI | p |")
    out.append("|---|---:|---:|---:|---:|---:|---:|")
    for d in domains:
        r = compare([g for g in a if g["domain"] == d], [g for g in b if g["domain"] == d], "correct")
        if r["n"] == 0:
            continue
        out.append(f"| {d} | {r['n']} | {_fmt_pct(r['a'])} | {_fmt_pct(r['b'])} | "
                   f"{100 * r['diff']:+.1f} pt | [{100 * r['ci'][0]:+.1f}, {100 * r['ci'][1]:+.1f}] | {r['p']:.3f} |")
    return "\n".join(out) + "\n"


# -- main ----------------------------------------------------------------------


def _label(path: Path) -> str:
    return f"{path.parent.name}/{path.stem}" if path.parent.name else path.stem


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("results", type=Path)
    parser.add_argument("--compare", type=Path, default=None, help="results.jsonl of the other condition (side B)")
    parser.add_argument("--md", nargs="?", const="", default=None, metavar="PATH",
                        help="also write the markdown to a file (default <results>.grade.md when PATH is omitted)")
    parser.add_argument("--graded", type=Path, default=None, help="write per-question grading jsonl, one line each")
    parser.add_argument("--questions", type=Path, default=QUESTIONS_DIR,
                        help=f"directory of the question jsonl files (default {QUESTIONS_DIR}). Grading uses the current version there")
    args = parser.parse_args()

    questions = load_questions(args.questions)
    if not questions:
        print(f"[grade] warning: no questions found in {args.questions}; grading from result snapshots", file=sys.stderr)
    a = [grade_row(r, questions) for r in load_rows(args.results)]
    text = render_report(_label(args.results), a)
    if args.compare:
        b = [grade_row(r, questions) for r in load_rows(args.compare)]
        text += "\n" + render_report(_label(args.compare), b)
        text += "\n" + render_compare(_label(args.results), _label(args.compare), a, b)
    print(text)

    if args.md is not None:
        md_path = Path(args.md) if args.md else args.results.with_suffix(".grade.md")
        md_path.write_text(text, encoding="utf-8")
        print(f"[md] -> {md_path}", file=sys.stderr)
    if args.graded:
        with args.graded.open("w", encoding="utf-8") as fh:
            for g in a:
                fh.write(json.dumps(g, ensure_ascii=False) + "\n")
        print(f"[graded] -> {args.graded}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
