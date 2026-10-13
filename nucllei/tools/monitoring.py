"""
title: Monitoring
description: 推論基盤の監視 (GPU / 推論エンジン / sandbox) の現在値と時系列 CSV を返す (管理者用)
"""

# **This toolkit is created as public right after sync** (scripts/nucllei/sync.py: create does not
# send access_control = visible to all users). After syncing, lower access_control to the admin
# group (read) in the nucllei admin UI (same for the skill `monitoring`).
# The `__user__.role != "admin"` guard at call time is the last line of defense, separate from UI visibility.
#
# A thin HTTP client hitting cage's `mon` domain (`POST /mon/now` / `POST /mon/export`).
# cage queries Prometheus and returns the results of named queries; now is formatted here into a Markdown
# table. export puts the CSV via the same route as the builtin `send_to_workspace` (sandbox `/files/write`)
# at `save_to` in the calling user's sandbox (an absolute `/workspace/…` path the model passes, inside the work area) and returns that path
# (all time-axis analysis is done with pandas in run_python). The raw CSV is not in the return value.
# Not placed under imports/ because imports/ exports/ are caches purged 30 days after the last update
# (nucllei #149), and this CSV has no original in Files so it cannot be restored with send_to_workspace(file_id).
# The tool does not decide the location (until 2026-10-07 it was fixed to `/workspace/monitoring/` → date folder) because the work area
# is "1 task = `/workspace/<YYYYMMDD>-<task name>/`" and only the model knows the task name. save_to validation follows
# the same rules and wording as the other HTTP tools (tools live in the DB as separate modules, so nothing can be imported and it is copied).
# The list of metric names and how to read thresholds live in the skill `monitoring` (managed in 2 places together with cage's table —
# update both when adding a query).
# Model-visible text (docstring / return value) must not mention the name cage (CLAUDE.md).

import asyncio
import base64
import logging
import os
import posixpath
from typing import Any, Dict, List, Optional

import requests
from pydantic import BaseModel, Field

# The HTTP exit to the sandbox is the builtin sandbox_post only (do not hold the token wiring in 2 places).
# SANDBOX_URL is a constant that module reads from env — empty means a deployment without a sandbox.
from nucllei.builtin_tools.sandbox import SANDBOX_URL, sandbox_post
from nucllei.utils.image_refs import REF_ARGS_RULE, WORKSPACE_PATH_ARG_FORM
from nucllei.utils.tool_errors import ToolCallError, ToolError, ToolFailure, ToolFatal, ToolOutcome
from nucllei.utils.tool_render import Markdown

logger = logging.getLogger(__name__)

# Environment differences are absorbed by the nucllei container env (same rule as tools/doc.py).
CAGE_API_BASE = os.getenv("CAGE_API_BASE", "http://host.docker.internal:13303")
_WS = "/workspace"
# Files under exports/ are auto-attached to the chat after execution — rejected as intermediate work products
_WS_EXPORTS = "/workspace/exports"
# The form save_to accepts (reword WORKSPACE_PATH_ARG_FORM from nucllei `utils/image_refs` to say save_to)
_SAVE_TO_ARG_FORM = WORKSPACE_PATH_ARG_FORM.replace("path が受けるのは", "save_to が受けるのは", 1)

# Failures raise ToolError (nucllei #145, README_API §5.1 "Failures"). nucllei composes the text as
# `ERROR: {fact}。次の手: {next}`. fact is a statement of fact (may be relayed to the user as-is), next is
# an instruction only for the model. Plain style, no internal names (Prometheus URL / code). When naming a tool
# not in this toolkit (run_python) as the next step, make it conditional ("if available").
# kind: argument errors (names/group, 422, unknown_name, bad_request) = call / row count over the limit, no match = outcome /
# unreachable, Prometheus failure, other 4xx / 5xx, no sandbox / write failure, non-admin = failure /
# broken on our side (401/403 = auth / upstream config, 2xx body not parseable as JSON = deployment mismatch) = fatal.
# The HTTP exit and failure shaping share the same shape as doc.py (`_post` / `_error_info` / `_http_error` / `_json`).
_RETRY = "少し待って 1 回だけ再試行し、だめならこの事実をユーザーに伝える。"
_UNREACHABLE = "監視データのサーバーに接続できない"

# Statements of fact for no sandbox / write failure. sandbox_post's exception text contains env names
# and the sandbox's raw response, so it is not copied, and the HTTP status is not shown either (it stays in the log).
_NO_SANDBOX = "このサーバーにはコード実行環境 (sandbox) が無い"
_SANDBOX_WRITE_FAILED = "コード実行環境 (sandbox) へのファイルの書き込みに失敗した"


def _admin_only() -> ToolFailure:
    return ToolFailure("このツールは管理者だけが使える", "呼び直さない。")


def _error_info(r: Any) -> Dict[str, Any]:
    """JSON body of cage's failure response ({} if not JSON)."""
    try:
        info = r.json()
    except ValueError:
        return {}
    return info if isinstance(info, dict) else {}


def _http_error(r: Any, payload: Dict[str, Any]) -> ToolError:
    info = _error_info(r)
    code = info.get("error") or f"http_{r.status_code}"
    detail = str(info.get("detail") or "").strip()
    known = info.get("known") or []
    logger.warning(f"[monitoring] {r.status_code} {code}: {detail or r.text[:200]!r}")
    if r.status_code == 422:
        return ToolCallError(
            "引数の型が違う (since / step は文字列、names は文字列の配列)", "直して呼び直す。"
        )
    if code in ("prometheus_unavailable", "prometheus_error"):
        # detail contains Prometheus's internal URL or raw error text, so it is not copied
        return ToolFailure("監視データを取得できない", _RETRY)
    if code == "unknown_name":
        unknown = [n for n in payload.get("names") or [] if n not in known]
        fact = "指定した name が無い" + (f" ({', '.join(unknown)})" if unknown and known else "")
        if known:
            fact += f"。指定できる name: {', '.join(known)}"
        return ToolCallError(fact, "一覧の name で呼び直す。")
    if code == "too_many_rows":
        fact = "行数が上限を超える"
        if info.get("estimated") is not None:
            fact += f" (推定 {info['estimated']} 行)"
        return ToolOutcome(fact, "since を短くするか step を粗くして呼び直す。")
    if code == "bad_request" and detail:
        # cage's bad_request is an argument error (format / range / names/group). The text is model-facing Japanese
        if known:
            return ToolCallError(f"{detail}。指定できる値: {', '.join(known)}", "一覧の値で呼び直す。")
        if detail.startswith("since は最大"):
            return ToolCallError(detail, "since を 30d 以下にするか、期間を分けて呼ぶ。")
        return ToolCallError(detail, "直して呼び直す。")
    if r.status_code in (401, 403):
        # mon has no auth — a rejection is a deployment error (upstream proxy / config) that waiting will not fix
        return ToolFatal("監視データのサーバーがエラーを返した", _RETRY)
    return ToolFailure("監視データのサーバーがエラーを返した", _RETRY)


def _json(r: Any) -> Any:
    """JSON body of a 2xx. If unparseable, ToolFatal (deployment mismatch / relay anomaly)."""
    try:
        return r.json()
    except ValueError:
        logger.warning(f"[monitoring] response is not JSON: {r.text[:200]!r}")
        raise ToolFatal("監視データのサーバーの応答を解釈できない", _RETRY)


def _selector(names: Optional[List[str]], group: str) -> Dict[str, Any]:
    """Check that names / group are mutually exclusive and return the selector part sent to cage."""
    names = [n.strip() for n in (names or []) if n and n.strip()]
    group = (group or "").strip()
    if names and group:
        raise ToolCallError("names と group は同時に指定できない", "どちらか一方にする。")
    if not names and not group:
        raise ToolCallError("names も group も指定されていない", "どちらか一方を指定する。")
    return {"names": names} if names else {"group": group}


def _fmt(v: Any) -> str:
    """Format a single value briefly for a table cell (None / NaN → `—`)."""
    if v is None:
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if f != f:  # NaN
        return "—"
    if f == int(f) and abs(f) < 1e9:
        return str(int(f))
    if abs(f) >= 100:
        return f"{f:.0f}"
    if abs(f) >= 10:
        return f"{f:.1f}"
    return f"{f:.2f}"


def _label_key(labels: Dict[str, Any]) -> str:
    """Turn series labels into one string for a table column header (`gpu=0` → `0`; multiple → `k=v,k=v`)."""
    if not labels:
        return ""
    if len(labels) == 1:
        return str(next(iter(labels.values())))
    return ",".join(f"{k}={v}" for k, v in sorted(labels.items()))


def _now_table(results: List[Dict[str, Any]]) -> str:
    """Turn `/mon/now` results into a table. Labeled multi-series (GPU) use columns = labels;
    single series use 2 columns (metric | value). If mixed, emit both tables."""
    multi: List[Dict[str, Any]] = []
    scalar: List[Dict[str, Any]] = []
    for res in results:
        series = res.get("series") or []
        if len(series) > 1 or (series and series[0].get("labels")):
            multi.append(res)
        else:
            scalar.append(res)
    out: List[str] = []
    if multi:
        cols: List[str] = []
        label_name = "series"
        for res in multi:
            for s in res.get("series") or []:
                labels = s.get("labels") or {}
                k = _label_key(labels)
                if k not in cols:
                    cols.append(k)
                if len(labels) == 1 and label_name == "series":
                    label_name = next(iter(labels.keys()))
        out.append(f"| 指標 \\ {label_name} | " + " | ".join(cols) + " |")
        out.append("|---|" + "---:|" * len(cols))
        for res in multi:
            by_key = {_label_key(s.get("labels") or {}): s.get("value") for s in res.get("series") or []}
            unit = f" ({res['unit']})" if res.get("unit") else ""
            out.append(
                f"| {res.get('title') or res.get('name')}{unit} | "
                + " | ".join(_fmt(by_key.get(c)) for c in cols)
                + " |"
            )
        out.append("")
    if scalar:
        out.append("| 指標 | 値 |")
        out.append("|---|---:|")
        for res in scalar:
            series = res.get("series") or []
            v = series[0].get("value") if series else None
            unit = f" {res['unit']}" if res.get("unit") and v is not None else ""
            out.append(f"| {res.get('title') or res.get('name')} | {_fmt(v)}{unit} |")
        out.append("")
    return "\n".join(out)


def _resolve_save_to(raw: str) -> str:
    # Validate / normalize save_to and return the csv's absolute path (ToolCallError if unusable).
    # Only the absolute `/workspace/…` form is accepted. A bare name is never silently placed at a default location.
    # `..` is rejected per segment; `//` and `/./` are collapsed by normpath before judging exports/.
    def unusable(why: str) -> ToolCallError:
        return ToolCallError(
            f"save_to に使えない ({why}。指定: {raw!r})",
            "作業場所の `/workspace/<YYYYMMDD>-<作業名>/<名前>.csv` のように /workspace/ 配下の絶対パスで指定する "
            "(`..` と /workspace/exports/・memory/・recipes/ 配下は不可)。",
        )

    p = (raw or "").strip()
    if not p.startswith(_WS + "/"):
        raise ToolCallError(
            f"{p or '(空)'} は sandbox のパスの形でない", f"{_SAVE_TO_ARG_FORM} {REF_ARGS_RULE}"
        )
    if ".." in p.split("/"):
        raise unusable("`..` を含む")
    p = posixpath.normpath(p)
    if p == _WS or not p.startswith(_WS + "/") or (raw or "").strip().endswith("/"):
        raise unusable("ファイル名が無い")
    if p == _WS_EXPORTS or p.startswith(_WS_EXPORTS + "/"):
        raise unusable("exports/ に置くとチャットへ自動添付される")
    if any(p == f"{_WS}/{d}" or p.startswith(f"{_WS}/{d}/") for d in ("memory", "recipes")):
        raise unusable("memory/ recipes/ は残すものの置き場で、作業の出力は置かない")
    if not p.lower().endswith(".csv"):
        p += ".csv"
    return p


class Tools:
    # NOTE: nucllei shows the class docstring to the model as the toolkit-wide note — the dev-facing
    #   description goes in the comment at the top of the file (same as tools/doc.py).

    class Valves(BaseModel):
        api_base: str = Field(
            default=CAGE_API_BASE,
            description="監視 API (cage) の base URL。既定は nucllei の env (CAGE_API_BASE) を流用し、"
            "無ければ host.docker.internal:13303 (compose の extra_hosts で host を指す)",
        )
        timeout_sec: float = Field(
            default=30.0,
            description="HTTP request timeout in seconds (export は期間が長いと数秒かかる)",
        )

    def __init__(self) -> None:
        self.valves = self.Valves()

    # ----- internal -----

    def _post(
        self,
        route: str,
        payload: Dict[str, Any],
        *,
        timeout: Optional[float] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Any:
        """Hit cage and return a 2xx response. ToolFailure if unreachable; 4xx/5xx go through `_http_error`."""
        try:
            r = requests.post(
                f"{self.valves.api_base}{route}",
                json=payload,
                headers=headers,
                timeout=timeout or self.valves.timeout_sec,
            )
        except requests.RequestException as e:  # connection failure / timeout (the exception text contains internal URLs)
            logger.warning(f"[monitoring] {route} unreachable: {e!r}")
            raise ToolFailure(_UNREACHABLE, _RETRY)
        if r.status_code >= 400:
            raise _http_error(r, payload)
        return r

    # ----- functions called by the model -----

    def monitoring_now(
        self,
        names: Optional[List[str]] = None,
        group: str = "",
        __user__: Optional[dict] = None,
    ) -> Markdown:
        """指標の現在値を返す。`names` か `group` のどちらか一方を指定する。

        「今どうなっているか」を答えるときに使う。GPU のようにラベル (GPU 番号) を持つ指標は
        列がラベルの表、それ以外は「指標 | 値」の 2 列の表で返る (取得時刻とダッシュボードの URL つき)。
        値が `—` の指標はその時点のデータが無い。name の綴り違いは ERROR に指定できる name の一覧が出る。

        :param names: 取得する指標名のリスト (例: ["gpu_util", "gpu_temp"])。
        :param group: 取得するグループ名 ("gpu" / "sglang" / "sandbox")。`names` と同時には指定しない。
        :return: 取得時刻、Markdown 表、ダッシュボードの URL。値が `—` の指標はその時点のデータが無い。
                 失敗は ToolError を raise (name の綴り違いは call で、指定できる name の一覧を含む)。
        """
        if not __user__ or __user__.get("role") != "admin":
            raise _admin_only()
        payload = _selector(names, group)
        data = _json(self._post("/mon/now", payload))
        results = data.get("results") or []
        if not results:
            which = f"group: {payload['group']}" if "group" in payload else "names: " + ", ".join(payload["names"])
            raise ToolOutcome(
                f"該当する指標が無い ({which})", "指定できる name は skill `monitoring` の表を見る。"
            )
        head = f"取得時刻: {data.get('at', '')}" + (f" / group: {payload['group']}" if "group" in payload else "")
        dash = f"\nダッシュボード: [監視ダッシュボード]({data['grafana']})\n" if data.get("grafana") else ""
        return head + "\n\n" + _now_table(results) + dash

    async def monitoring_export(
        self,
        save_to: str,
        names: Optional[List[str]] = None,
        group: str = "",
        since: str = "1h",
        step: str = "",
        __user__: Optional[dict] = None,
    ) -> str:
        """指標の時系列を CSV として sandbox の `save_to` に置き、そのパスを返す。

        「さっきから」「最近」「一時的か続いているか」のように時間方向の判断が要るとき、
        および自分で集計・作図したいときに使う。戻り値に数値は含まれない。返ったパスを
        `run_python` の pandas で読む。CSV は `ts,time,name,labels,value` の縦持ち (`labels` は
        `gpu=0` の形)。行数が多すぎるときは ERROR が返る (「次の手:」に期間を狭めるか step を粗くする手が出る)。

        :param save_to: CSV の置き先。作業場所の `/workspace/<YYYYMMDD>-<作業名>/<名前>.csv` のように /workspace/ 配下の絶対パスを指定する (exports/・memory/・recipes/ は不可。同じ名前は上書き)。
        :param names: 書き出す指標名のリスト。
        :param group: 書き出すグループ名 ("gpu" / "sglang" / "sandbox" / "all")。`names` と同時には指定しない。
        :param since: どれだけ遡るか。Prometheus 形式の期間 (例: "15m", "1h", "6h", "24h")。既定 "1h"。
        :param step: 刻み幅 (例: "15s", "1m", "5m")。省略時はサーバ側が期間から決める。
        :return: 置いた CSV のパス (`save_to`。拡張子が無ければ `.csv` を足す)、行数、刻み、期間、列 (`ts,time,name,labels,value` の
                 縦持ち。`labels` は `gpu=0` の形)、pandas で読む 1 行。失敗は ToolError を raise
                 (行数が多すぎるときは outcome で、「次の手:」に期間を狭めるか step を粗くする手)。
        """
        if not __user__ or __user__.get("role") != "admin":
            raise _admin_only()
        if not SANDBOX_URL:
            raise ToolFailure(f"{_NO_SANDBOX}。時系列は取れない", "現在値なら monitoring_now を使う。")
        payload = _selector(names, group)
        # Check the destination's form before querying (do not hit the monitoring server with a wrong save_to)
        path = _resolve_save_to(save_to)
        since = (since or "1h").strip()
        step = (step or "").strip()
        payload["since"] = since
        if step:
            payload["step"] = step
        # requests is synchronous, so move it off the event loop
        r = await asyncio.to_thread(self._post, "/mon/export", payload)
        data = r.content or b""
        rows = r.headers.get("X-Mon-Rows") or str(max(0, data.count(b"\n") - 1))
        step_s = r.headers.get("X-Mon-Step-S") or ""

        try:
            await sandbox_post(
                "/files/write",
                {
                    "user_id": __user__["id"],
                    # the sandbox's /files/* takes workspace-relative paths (the sandbox side mkdirs the parent directory)
                    "path": path[len(_WS) + 1 :],
                    "b64": base64.b64encode(data).decode(),
                },
                timeout_s=120,
            )
        except Exception as e:  # noqa: BLE001 - sandbox_post exceptions (connection / HTTP) get fixed wording and go to the log
            logger.warning(f"[monitoring] export write failed: {e!r}")
            raise ToolFailure(_SANDBOX_WRITE_FAILED, _RETRY)
        return (
            f"path: {path}\n"
            f"rows: {rows} / step: {step_s} s / since: {since}\n"
            "columns: ts,time,name,labels,value (縦持ち。labels は `gpu=0` の形、無ければ空)\n"
            f'次の手: run_python が使えるなら df = pd.read_csv("{path}") → '
            'df.pivot_table(index="time", columns=["name","labels"], values="value") で横持ちにして集計する。\n'
        )
