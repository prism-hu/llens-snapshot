"""mon domain: returns Prometheus monitoring data via **named queries + server-side aggregation**.

The entry point for admins to ask about GPU / SGLang / sandbox state from chat.
Design notes in ../../docs/cage/23_mon.md, configuration in ../README.md, "mon domain" section.

Lines we hold:

- **No PromQL is accepted from requests**. Only names (`gpu_util` etc.) are accepted; the
  expressions are confined to `QUERIES` in this module. cage :13303 is reachable without
  auth, so passing raw PromQL would let anyone pull every Prometheus series
- **Raw series are not streamed into the model's context**. `/mon/now` is latest values only.
  When a time series is needed, hand the `/mon/export` CSV to the sandbox and look at it with
  pandas (not loaded into context). Aggregation / summarization is the job of Python on the
  sandbox side; cage holds no aggregation code
  (the story of adding and removing `/mon/range` is in ../../docs/cage/23_mon.md)
- **Access control is not cage's responsibility**. The ACL of the tool / skill on the nucllei
  side (admin only) guards it. Nothing secret lives here; only operational stats are returned
- Prometheus is reached with stdlib urllib (no client library, since deps cannot be added in
  the air-gapped site)
"""

from __future__ import annotations

import csv
import datetime as _dt
import io
import json
import logging
import math
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

logger = logging.getLogger("cage.mon")

router = APIRouter()

# --- Target --------------------------------------------------------------------
# Prometheus is exposed by compose's monitoring profile at 127.0.0.1:13401 on the host
# (README_monitoring.md). cage runs on the host, so it reaches it directly.
PROM_BASE = os.environ.get("CAGE_MON_PROM_BASE", "http://127.0.0.1:13401").rstrip("/")
TIMEOUT_S = float(os.environ.get("CAGE_MON_TIMEOUT_S", "10"))


def derive_grafana_base(explicit: str) -> str:
    """Grafana base for human-facing links.

    Used only when `CAGE_GRAFANA_BASE` is set explicitly (trailing `/` dropped). Unset means
    empty = no links emitted. Not derived from the LLENS entry point or similar (decided
    2026-10-03 by <redacted> — do not guess per-deployment addresses).
    """
    return (explicit or "").strip().rstrip("/")


GRAFANA_BASE = derive_grafana_base(os.environ.get("CAGE_GRAFANA_BASE", ""))

# --- Named queries ------------------------------------------------------------
# 1 name = 1 entry. Expressions are the same as the panels in monitoring/grafana/dashboards/*.json
# (only label-dropping aggregation added). `labels` are the label names kept in the response
# (others dropped), `scale` is a factor applied to the Prometheus value (unit conversion).
# To add a name, add one line here.
QUERIES: list[dict[str, Any]] = [
    # gpu (DCGM exporter; gpu label = 0..7)
    {"name": "gpu_util", "group": "gpu", "title": "GPU 使用率", "unit": "%",
     "description": "GPU ごとの演算コア使用率 (DCGM)。",
     "promql": "max by (gpu) (DCGM_FI_DEV_GPU_UTIL)", "labels": ["gpu"]},
    {"name": "gpu_temp", "group": "gpu", "title": "GPU 温度", "unit": "°C",
     "description": "GPU ごとのコア温度。83°C 超でスロットリングの目安。",
     "promql": "max by (gpu) (DCGM_FI_DEV_GPU_TEMP)", "labels": ["gpu"]},
    {"name": "gpu_vram_used", "group": "gpu", "title": "VRAM 使用量", "unit": "GiB",
     "description": "GPU ごとのフレームバッファ使用量 (1 基 141 GB)。",
     "promql": "max by (gpu) (DCGM_FI_DEV_FB_USED)", "labels": ["gpu"], "scale": 1 / 1024},
    {"name": "gpu_power", "group": "gpu", "title": "GPU 電力", "unit": "W",
     "description": "GPU ごとの消費電力。",
     "promql": "max by (gpu) (DCGM_FI_DEV_POWER_USAGE)", "labels": ["gpu"]},
    # sglang (inference server; instance / rank labels dropped, collapsed to one series)
    {"name": "sglang_running", "group": "sglang", "title": "同時実行リクエスト数", "unit": "req",
     "description": "SGLang が今処理中のリクエスト数。",
     "promql": "sum(sglang:num_running_reqs)", "labels": []},
    {"name": "sglang_queue", "group": "sglang", "title": "待ちキュー長", "unit": "req",
     "description": "SGLang の実行待ちリクエスト数。増え続けるなら飽和。",
     "promql": "sum(sglang:num_queue_reqs)", "labels": []},
    {"name": "sglang_gen_throughput", "group": "sglang", "title": "生成スループット", "unit": "tok/s",
     "description": "SGLang が報告する生成トークン速度。",
     "promql": "sum(sglang:gen_throughput)", "labels": []},
    {"name": "sglang_ttft_p50", "group": "sglang", "title": "TTFT p50", "unit": "s",
     "description": "初回トークンまでの時間の中央値 (直近 5 分)。",
     "promql": "histogram_quantile(0.50, sum by (le) (rate(sglang:time_to_first_token_seconds_bucket[5m])))",
     "labels": []},
    {"name": "sglang_ttft_p95", "group": "sglang", "title": "TTFT p95", "unit": "s",
     "description": "初回トークンまでの時間の 95 パーセンタイル (直近 5 分)。3 秒超で要注意。",
     "promql": "histogram_quantile(0.95, sum by (le) (rate(sglang:time_to_first_token_seconds_bucket[5m])))",
     "labels": []},
    {"name": "sglang_ttft_p99", "group": "sglang", "title": "TTFT p99", "unit": "s",
     "description": "初回トークンまでの時間の 99 パーセンタイル (直近 5 分)。",
     "promql": "histogram_quantile(0.99, sum by (le) (rate(sglang:time_to_first_token_seconds_bucket[5m])))",
     "labels": []},
    {"name": "sglang_prompt_tok_rate", "group": "sglang", "title": "入力トークン処理速度", "unit": "tok/s",
     "description": "プロンプト (入力) トークンの処理速度 (直近 5 分平均)。",
     "promql": "sum(rate(sglang:prompt_tokens_total[5m]))", "labels": []},
    {"name": "sglang_gen_tok_rate", "group": "sglang", "title": "出力トークン生成速度", "unit": "tok/s",
     "description": "生成 (出力) トークンの速度 (直近 5 分平均)。",
     "promql": "sum(rate(sglang:generation_tokens_total[5m]))", "labels": []},
    {"name": "sglang_kv_used_tokens", "group": "sglang", "title": "KV キャッシュ使用トークン数", "unit": "tok",
     "description": "KV キャッシュに載っているトークン数。",
     "promql": "sum(sglang:num_used_tokens)", "labels": []},
    {"name": "sglang_cache_hit_ratio", "group": "sglang", "title": "プレフィックスキャッシュヒット率", "unit": "%",
     "description": "プロンプトのうちキャッシュから再利用できた割合 (直近 5 分)。",
     "promql": ("sum(rate(sglang:realtime_tokens_total{mode=\"prefill_cache\"}[5m])) / "
                "(sum(rate(sglang:realtime_tokens_total{mode=\"prefill_cache\"}[5m])) + "
                "sum(rate(sglang:realtime_tokens_total{mode=\"prefill_compute\"}[5m])))"),
     "labels": [], "scale": 100},
    {"name": "sglang_e2e_p50", "group": "sglang", "title": "リクエスト完了時間 p50", "unit": "s",
     "description": "リクエスト受付から完了までの時間の中央値 (直近 5 分)。",
     "promql": "histogram_quantile(0.50, sum by (le) (rate(sglang:e2e_request_latency_seconds_bucket[5m])))",
     "labels": []},
    {"name": "sglang_e2e_p95", "group": "sglang", "title": "リクエスト完了時間 p95", "unit": "s",
     "description": "リクエスト受付から完了までの時間の 95 パーセンタイル (直近 5 分)。",
     "promql": "histogram_quantile(0.95, sum by (le) (rate(sglang:e2e_request_latency_seconds_bucket[5m])))",
     "labels": []},
    # sandbox (code execution platform; what the metrics mean is in the nucllei repo's sandbox/docs/health.md)
    {"name": "sandbox_loop_used", "group": "sandbox", "title": "loop デバイス使用数", "unit": "",
     "description": "sandbox が使用中の loop デバイス数。",
     "promql": "sum(sandbox_loop_used)", "labels": []},
    {"name": "sandbox_loop_nodes_total", "group": "sandbox", "title": "loop デバイス総数", "unit": "",
     "description": "sandbox に用意されている loop デバイス数 (上限)。",
     "promql": "sum(sandbox_loop_nodes_total)", "labels": []},
    {"name": "sandbox_disk_used", "group": "sandbox", "title": "workspace ディスク使用量", "unit": "GiB",
     "description": "/workspaces のディスク使用量。",
     "promql": "sum(sandbox_disk_used_bytes)", "labels": [], "scale": 1 / 2**30},
    {"name": "sandbox_disk_total", "group": "sandbox", "title": "workspace ディスク容量", "unit": "GiB",
     "description": "/workspaces のディスク総容量。",
     "promql": "sum(sandbox_disk_total_bytes)", "labels": [], "scale": 1 / 2**30},
    {"name": "sandbox_workspaces", "group": "sandbox", "title": "workspace 数", "unit": "",
     "description": "存在する workspace (.img) の数。",
     "promql": "sum(sandbox_workspaces_total)", "labels": []},
    {"name": "sandbox_sessions_active", "group": "sandbox", "title": "アクティブセッション数", "unit": "",
     "description": "実行中の sandbox セッション数。",
     "promql": "sum(sandbox_sessions_active)", "labels": []},
]

BY_NAME: dict[str, dict[str, Any]] = {q["name"]: q for q in QUERIES}
GROUPS: list[str] = list(dict.fromkeys(q["group"] for q in QUERIES))

# Grafana dashboards for human-facing links (uids are in monitoring/grafana/dashboards/*.json).
# The GPU panels live in the lower part of the SGLang dashboard, so gpu → sglang-h200.
DASHBOARD_UID: dict[str, str] = {
    "gpu": "sglang-h200",
    "sglang": "sglang-h200",
    "sandbox": "llens-sandbox",
}

# --- Limits ----------------------------------------------------------------------
DEFAULT_SINCE = "1h"
MAX_SINCE_S = 30 * 24 * 3600
# export (full-resolution CSV). The scrape interval is 5s per monitoring/prometheus.yml.
SCRAPE_INTERVAL_S = 5
EXPORT_MAX_ROWS = 500_000
# Prometheus query_range caps at 11,000 points per series, so longer ranges are
# fetched in time-split chunks
PROM_MAX_RESOLUTION = 11_000

_DURATION_RE = re.compile(r"^(\d+)([smhdw])$")
_UNIT_S = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}


def parse_duration(text: str, name: str = "since") -> int:
    """Prometheus-style duration (`30m` / `1h` / `7d`) → seconds. ValueError if invalid (the message names the argument via `name`)."""
    m = _DURATION_RE.match((text or "").strip())
    if not m:
        raise ValueError(f"{name} は <数字><s|m|h|d|w> の形 (例 30m, 1h, 7d)。指定: {text}")
    n, unit = int(m.group(1)), m.group(2)
    if n <= 0:
        raise ValueError(f"{name} が正の長さでない (指定: {text})")
    return n * _UNIT_S[unit]


class CatalogRequest(BaseModel):
    pass


class NowRequest(BaseModel):
    names: list[str] | None = None
    group: str | None = None


class ExportRequest(BaseModel):
    names: list[str] | None = None
    group: str | None = None
    since: str | None = None
    step: str | None = None


# The bad_request detail is placed verbatim by nucllei/tools/monitoring.py into the ERROR's
# "statement of fact" (the tool adds the next step), so write what happened, not instructions.
def _err(status: int, error: str, detail: str, **extra: Any) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": error, "detail": detail, **extra})


class PrometheusUnavailable(Exception):
    pass


class PrometheusError(Exception):
    pass


def _prom_get(path: str, params: dict[str, Any]) -> dict[str, Any]:
    """Call the Prometheus HTTP API and return `data`. Tests monkeypatch this."""
    url = f"{PROM_BASE}{path}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_S) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # 4xx/5xx. The body usually carries Prometheus's error message as JSON
        try:
            body = json.loads(e.read().decode("utf-8"))
            detail = body.get("error") or str(e)
        except Exception:  # noqa: BLE001
            detail = str(e)
        raise PrometheusError(f"HTTP {e.code}: {detail}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise PrometheusUnavailable(f"{PROM_BASE} に届かない: {getattr(e, 'reason', e)}") from e
    except ValueError as e:
        raise PrometheusError(f"応答が JSON でない: {e}") from e
    if payload.get("status") != "success":
        raise PrometheusError(str(payload.get("error") or payload))
    return payload.get("data") or {}


def _to_float(raw: Any) -> float | None:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return v


def _round(v: float | None) -> float | None:
    if v is None:
        return None
    return round(v, 3)


def _project_labels(metric: dict[str, str], keep: list[str]) -> dict[str, str]:
    return {k: metric[k] for k in keep if k in metric}


def _sort_key(labels: dict[str, str]) -> tuple:
    # Sort numeric labels numerically so gpu="10" does not come before "2"
    return tuple((int(v) if v.isdigit() else v) for v in labels.values())


def _now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _grafana_url(group: str) -> str | None:
    if not GRAFANA_BASE:
        return None
    return f"{GRAFANA_BASE}/d/{DASHBOARD_UID[group]}"


# --- endpoints -----------------------------------------------------------------


@router.post("/mon/catalog")
def mon_catalog(req: CatalogRequest) -> Any:
    """List of named queries. PromQL is not exposed (descriptions are prose only)."""
    return {
        "queries": [
            {k: q[k] for k in ("name", "group", "title", "unit", "description")} for q in QUERIES
        ],
        "groups": GROUPS,
        "grafana": {
            g: GRAFANA_BASE + "/d/" + uid
            for g, uid in DASHBOARD_UID.items()
            if GRAFANA_BASE and g != "gpu"
        },
    }


def _resolve_names(
    names: list[str] | None, group: str | None, allow_all: bool = False
) -> list[dict[str, Any]] | JSONResponse:
    """Look up query table rows from exactly one of `names` or `group`."""
    if (names is None) == (group is None):
        return _err(400, "bad_request", "names と group は一方だけ指定できる (両方あるか、どちらも無い)")
    if group is not None:
        if allow_all and group == "all":
            return list(QUERIES)
        if group not in GROUPS:
            known = GROUPS + (["all"] if allow_all else [])
            return _err(400, "bad_request", f"group が不明: {group}", known=known)
        return [q for q in QUERIES if q["group"] == group]
    if not names:
        return _err(400, "bad_request", "names が空")
    unknown = [n for n in names if n not in BY_NAME]
    if unknown:
        return _err(400, "unknown_name", f"不明な名前: {', '.join(unknown)}", known=list(BY_NAME))
    # Drop duplicates, keep the requested order
    return [BY_NAME[n] for n in dict.fromkeys(names)]


def _instant(q: dict[str, Any]) -> list[dict[str, Any]]:
    data = _prom_get("/api/v1/query", {"query": q["promql"]})
    scale = q.get("scale", 1)
    series = []
    for item in data.get("result") or []:
        v = _to_float(item.get("value", [None, None])[1])
        series.append({
            "labels": _project_labels(item.get("metric") or {}, q["labels"]),
            "value": _round(v * scale) if v is not None else None,
        })
    series.sort(key=lambda s: _sort_key(s["labels"]))
    if not series:
        series = [{"labels": {}, "value": None}]
    return series


@router.post("/mon/now")
def mon_now(req: NowRequest) -> Any:
    """Latest values for the given names (or a whole group)."""
    queries = _resolve_names(req.names, req.group)
    if isinstance(queries, JSONResponse):
        return queries
    results = []
    try:
        for q in queries:
            results.append({
                "name": q["name"],
                "title": q["title"],
                "unit": q["unit"],
                "series": _instant(q),
            })
    except PrometheusUnavailable as e:
        return _err(502, "prometheus_unavailable", str(e))
    except PrometheusError as e:
        return _err(502, "prometheus_error", str(e))
    return {
        "at": _now_iso(),
        "results": results,
        "grafana": _grafana_url(queries[0]["group"]),
    }


# --- export (full-resolution CSV) ---------------------------------------------------


def default_export_step(since_s: int) -> int:
    """Default step: the scrape interval for up to 1h, otherwise since/2000 (rounded up, floor 5s)."""
    if since_s <= 3600:
        return SCRAPE_INTERVAL_S
    return max(SCRAPE_INTERVAL_S, math.ceil(since_s / 2000))


def _labels_str(labels: dict[str, str]) -> str:
    return ";".join(f"{k}={v}" for k, v in labels.items())


def _query_range_chunked(promql: str, start: int, end: int, step: int) -> list[dict[str, Any]]:
    """Fetch query_range in windows that fit the 11,000 points/series cap and concatenate per series."""
    window = PROM_MAX_RESOLUTION * step
    merged: dict[str, dict[str, Any]] = {}
    cur = start
    while cur <= end:
        chunk_end = min(cur + window - step, end)
        data = _prom_get("/api/v1/query_range", {
            "query": promql, "start": cur, "end": chunk_end, "step": step,
        })
        for item in data.get("result") or []:
            key = json.dumps(item.get("metric") or {}, sort_keys=True)
            slot = merged.setdefault(key, {"metric": item.get("metric") or {}, "values": []})
            slot["values"].extend(item.get("values") or [])
        cur = chunk_end + step
    return list(merged.values())


@router.post("/mon/export")
def mon_export(req: ExportRequest) -> Any:
    """Return the full-resolution time series as long-format CSV (for pandas in the sandbox; no downsampling)."""
    queries = _resolve_names(req.names, req.group, allow_all=True)
    if isinstance(queries, JSONResponse):
        return queries
    try:
        since_s = parse_duration(req.since or DEFAULT_SINCE)
        step_s = parse_duration(req.step, "step") if req.step else default_export_step(since_s)
    except ValueError as e:
        return _err(400, "bad_request", str(e))
    # Do not silently clamp to 30d (the tool reports the requested since, so clamping would be
    # misread as "fetched the requested period"). Return 400 and let the caller decide.
    if since_s > MAX_SINCE_S:
        return _err(400, "bad_request", f"since は最大 {MAX_SINCE_S // 86400}d (指定: {req.since})")

    to = _dt.datetime.now().astimezone().replace(microsecond=0)
    frm = to - _dt.timedelta(seconds=since_s)
    start, end = int(frm.timestamp()), int(to.timestamp())
    samples_per_series = since_s // step_s + 1
    hint = "step を大きくするか since を短くする"

    try:
        # Estimate the row count from the series count (number of latest values) before fetching
        counts = {
            q["name"]: len(_prom_get("/api/v1/query", {"query": q["promql"]}).get("result") or [])
            for q in queries
        }
        estimated = sum(counts.values()) * samples_per_series
        if estimated > EXPORT_MAX_ROWS:
            return _err(
                400, "too_many_rows",
                f"見積り {estimated} 行が上限 {EXPORT_MAX_ROWS} 行を超える",
                estimated=estimated, hint=hint,
            )
        rows: list[tuple[str, str, int, float | None]] = []
        for q in queries:
            scale = q.get("scale", 1)
            for item in _query_range_chunked(q["promql"], start, end, step_s):
                labels = _labels_str(_project_labels(item["metric"], q["labels"]))
                for ts, raw in item["values"]:
                    v = _to_float(raw)
                    rows.append((q["name"], labels, int(float(ts)), v * scale if v is not None else None))
                    if len(rows) > EXPORT_MAX_ROWS:
                        return _err(
                            400, "too_many_rows",
                            f"上限 {EXPORT_MAX_ROWS} 行を超えた",
                            estimated=len(rows), hint=hint,
                        )
    except PrometheusUnavailable as e:
        return _err(502, "prometheus_unavailable", str(e))
    except PrometheusError as e:
        return _err(502, "prometheus_error", str(e))

    rows.sort(key=lambda r: (r[0], r[1], r[2]))
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["ts", "time", "name", "labels", "value"])
    for name, labels, ts, v in rows:
        t = _dt.datetime.fromtimestamp(ts).astimezone().isoformat()
        w.writerow([ts, t, name, labels, "" if v is None else repr(v)])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"X-Mon-Rows": str(len(rows)), "X-Mon-Step-S": str(step_s)},
    )
