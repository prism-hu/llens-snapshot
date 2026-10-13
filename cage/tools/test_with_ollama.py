"""End-to-end behavior test: ollama (gpt-oss:20b) に tool spec を渡して、
モデルが期待通りの tool call を生成するか検証する。

このテストは AI モデル(ローカル ollama)が実装の品質契約に従うかを確認する。
- 最初に list_doc を呼ぶ(read_doc を捏造 path で呼ばない)
- arguments は spec に沿う(path のみ、`query` 等を勝手に作らない)
- child_count > 0 の応答に対して drill-down する(再度 list_doc を呼ぶ)
- list_doc で関連 CQ が見えたら必ず read_doc まで呼ぶ
  (lookup だけで終わらない)

ollama (localhost:11434) と gpt-oss:20b が無ければ skip。
モデル呼び出しは数十秒かかるので時間に余裕を持って実行する。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import requests

OLLAMA_URL = "http://localhost:11434"
MODEL = "gpt-oss:20b"
TIMEOUT = 300

# ---------------------------------------------------------------------------
# OpenAI 形式の tool spec(Open WebUI が `tool/doc_tool.py` から自動生成
# するのと同じ形を手書きで再現)
# ---------------------------------------------------------------------------

TOOL_SPECS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_doc",
            "description": (
                "ドキュメントツリーのノード一覧を取得する。read_doc より必ず先に呼ぶこと。"
                "返り値の children に含まれる `path` (フルパス文字列) をそのまま次の "
                "list_doc や read_doc の引数として使う。"
                "child_count > 0 なのに children が空なら、同じ path で list_doc を再度呼ぶ。"
                "存在しない path を渡すと 404 エラー dict が返る (siblings_at_parent 付き)。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "ノードへのフルパス文字列。先頭は必ず `/`。"
                            "省略時は root (`/`)。例: `/`, `/肺癌_2024-小細胞癌`, "
                            "`/肺癌_2024-小細胞癌/CQ1`"
                        ),
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_doc",
            "description": (
                "指定ノードの本文(Markdown)を取得する。呼ぶ前に必ず list_doc で path を確認すること。"
                "path は list_doc の children にある `path` をそのまま使う。"
                "存在しない path を渡すと、本文の代わりにエラー文字列が返る。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "list_doc で確認したフルパス文字列。例: `/肺癌_2024-小細胞癌/CQ1`"
                        ),
                    },
                },
                "required": ["path"],
            },
        },
    },
]

SYSTEM_PROMPT = (
    "あなたは診療ガイドライン検索のアシスタント。"
    "Doc tool で path を辿って探索 → 本文を取得して引用付きで答える。"
    "path は `/` 始まりのフルパス文字列。list_doc の children に含まれる `path` を"
    "そのまま次の引数に使うこと。"
)


# ---------------------------------------------------------------------------
# skip ガード
# ---------------------------------------------------------------------------


def _ollama_available() -> bool:
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=2)
    except Exception:
        return False
    if r.status_code != 200:
        return False
    names = [m["name"] for m in r.json().get("models", [])]
    return MODEL in names


pytestmark = pytest.mark.skipif(
    not _ollama_available(),
    reason=f"ollama or {MODEL} not available on {OLLAMA_URL}",
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def chat(messages: list[dict], tools: list[dict] = TOOL_SPECS) -> dict:
    body = {
        "model": MODEL,
        "messages": messages,
        "tools": tools,
        "stream": False,
        "options": {"temperature": 0.0},
    }
    r = requests.post(f"{OLLAMA_URL}/api/chat", json=body, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()["message"]


def parse_args(tool_call: dict) -> dict:
    """ollama 応答中の function.arguments は dict / str どちらの形もある。"""
    args = tool_call["function"].get("arguments", {})
    if isinstance(args, str):
        args = json.loads(args)
    return args


def _fake_root() -> dict:
    """root の list_doc 応答(`/`、セクション一覧)。"""
    return {
        "path": "/",
        "title": "知識ベース",
        "meta": {},
        "children": [
            {
                "path": "/肺癌_2024-小細胞癌",
                "title": "小細胞肺癌",
                "children": [],
                "child_count": 18,
            },
            {
                "path": "/肺癌_2024-非小細胞癌",
                "title": "非小細胞肺癌",
                "children": [],
                "child_count": 73,
            },
            {
                "path": "/肺癌_2024-診断",
                "title": "診断",
                "children": [],
                "child_count": 27,
            },
        ],
        "child_count": 3,
    }


def _fake_section_nsclc() -> dict:
    """非小細胞癌セクションの CQ 一覧 (EGFR 一次治療を含む)。"""
    return {
        "path": "/肺癌_2024-非小細胞癌",
        "title": "非小細胞肺癌",
        "meta": {"guideline": "肺癌診療ガイドライン 2024年版"},
        "children": [
            {
                "path": "/肺癌_2024-非小細胞癌/CQ45",
                "title": "ⅢB-Ⅳ期 EGFR/ALK/ROS1 等のドライバ陰性 PS 0-1 の一次治療",
                "children": [],
                "child_count": 0,
            },
            {
                "path": "/肺癌_2024-非小細胞癌/CQ46",
                "title": "ⅢB-Ⅳ期 EGFR陽性 PS 0-1 の一次治療",
                "children": [],
                "child_count": 0,
            },
            {
                "path": "/肺癌_2024-非小細胞癌/CQ47",
                "title": "ⅢB-Ⅳ期 EGFR陽性 PS 2-4 の一次治療",
                "children": [],
                "child_count": 0,
            },
        ],
        "child_count": 3,
    }


def _fake_for(path: str) -> dict | str:
    """path に対するモック応答(知識 API の真似)。read 用には markdown 文字列を返す。"""
    if path in ("", "/"):
        return _fake_root()
    if path == "/肺癌_2024-非小細胞癌":
        return _fake_section_nsclc()
    if path.startswith("/肺癌_2024-非小細胞癌/CQ"):
        # read_doc 想定。本文 (markdown) を返す。
        return (
            "## 推奨\n"
            "EGFR陽性ⅢB-Ⅳ期 PS 0-1 の一次治療として、オシメルチニブ単剤を"
            "強く推奨する(推奨の強さ: 1、エビデンスレベル: A)。\n"
            "## 解説\n"
            "FLAURA 試験で...\n"
        )
    return {
        "error": "not_found",
        "missing": path.split("/")[-1] if "/" in path else path,
        "valid_path": "/",
        "siblings_at_parent": ["肺癌_2024-小細胞癌", "肺癌_2024-非小細胞癌"],
    }


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


def test_first_call_is_lookup_not_read() -> None:
    """ユーザクエリに対して、最初に呼ぶのは list_doc(read_doc を捏造 path で呼ばない)。"""
    msg = chat([
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "肺癌のガイドラインで EGFR の1次治療について教えて"},
    ])
    tool_calls = msg.get("tool_calls", [])
    assert tool_calls, f"no tool_calls; content={msg.get('content', '')[:200]}"
    fn = tool_calls[0]["function"]["name"]
    assert fn == "list_doc", f"first call should be list_doc, got {fn}"


def test_first_lookup_uses_root_path() -> None:
    """初手の list_doc は path 省略 か `/` 始まりの文字列。"""
    msg = chat([
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "肺癌のガイドラインで EGFR について教えて"},
    ])
    args = parse_args(msg["tool_calls"][0])
    path = args.get("path", "/")
    assert isinstance(path, str), f"path must be string, got {type(path).__name__}"
    assert path == "" or path.startswith("/"), (
        f"path must start with '/', got {path!r}"
    )


def test_arguments_only_use_spec_keys() -> None:
    """spec にない `query` や `q` 等を勝手に作らない。"""
    msg = chat([
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "EGFR の1次治療"},
    ])
    args = parse_args(msg["tool_calls"][0])
    allowed = {"path"}
    extra = set(args.keys()) - allowed
    assert not extra, f"unknown arguments emitted: {extra} (full args: {args})"


def test_self_corrects_on_not_found_error() -> None:
    """間違った path を試して 404 エラー応答(siblings_at_parent 付き)を見せたら、
    siblings から正しい path を選んで再試行できるか。"""
    user = {"role": "user", "content": "肺癌の小細胞肺癌について教えて"}
    sys = {"role": "system", "content": SYSTEM_PROMPT}

    msg1 = chat([sys, user])
    assert msg1.get("tool_calls"), "expected first list_doc"
    first_call_id = msg1["tool_calls"][0].get("id", "0")

    # 最初の list_doc には誤った path への 404 を返してみる
    fake_404 = {
        "error": "not_found",
        "missing": "小細胞肺癌",
        "valid_path": "/",
        "siblings_at_parent": [
            "肺癌_2024-小細胞癌",
            "肺癌_2024-非小細胞癌",
            "肺癌_2024-診断",
        ],
    }

    msg2 = chat([
        sys, user, msg1,
        {
            "role": "tool",
            "tool_call_id": first_call_id,
            "content": json.dumps(fake_404, ensure_ascii=False),
        },
    ])
    assert msg2.get("tool_calls"), (
        f"expected retry call after 404; content={msg2.get('content','')[:300]}"
    )
    second_call = msg2["tool_calls"][0]
    args = parse_args(second_call)
    path = args.get("path", "")
    # siblings から正しい id (`肺癌_2024-小細胞癌`) を選んで使うことを期待
    assert "肺癌_2024-小細胞癌" in path, (
        f"expected path to use 肺癌_2024-小細胞癌 (from siblings), got {path!r}"
    )


def test_drills_down_when_child_count_positive() -> None:
    """list_doc の応答に child_count > 0 / children=[] が来たら、再度 list_doc を呼ぶ。"""
    user = {"role": "user", "content": "肺癌の小細胞肺癌について教えて"}
    sys = {"role": "system", "content": SYSTEM_PROMPT}

    msg1 = chat([sys, user])
    assert msg1.get("tool_calls"), "expected tool_calls in first response"
    first = msg1["tool_calls"][0]
    assert first["function"]["name"] == "list_doc"

    tool_call_id = first.get("id", "0")
    msg2 = chat([
        sys,
        user,
        msg1,
        {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": json.dumps(_fake_root(), ensure_ascii=False),
        },
    ])

    next_calls = msg2.get("tool_calls", [])
    assert next_calls, (
        f"expected drill-down tool_call; content={msg2.get('content','')[:300]}"
    )
    next_call = next_calls[0]
    assert next_call["function"]["name"] == "list_doc", (
        f"expected drill-down via list_doc, got {next_call['function']['name']}"
    )
    args = parse_args(next_call)
    path = args.get("path", "")
    assert path == "/肺癌_2024-小細胞癌", (
        f"expected path /肺癌_2024-小細胞癌, got {path!r}"
    )


def test_calls_both_list_and_read_in_one_session() -> None:
    """1 セッション中で list_doc だけで終わらず、関連 CQ に read_doc まで呼ぶか。

    ユーザの質問に明らかに該当しそうな CQ がリストに見えたら、
    タイトルだけで回答せず必ず本文を取りに行くことを契約とする。
    """
    user = {
        "role": "user",
        "content": "EGFR陽性のⅢB-Ⅳ期非小細胞肺癌 PS 0-1 に対する一次治療の推奨は何？",
    }
    sys = {"role": "system", "content": SYSTEM_PROMPT}
    messages: list[dict[str, Any]] = [sys, user]

    saw_list = False
    saw_read_path: str | None = None

    # 最大 6 回まで model に応答してもらう (list → list → read で十分終わる想定)
    for _ in range(6):
        msg = chat(messages)
        tool_calls = msg.get("tool_calls", [])
        if not tool_calls:
            break
        messages.append(msg)
        for tc in tool_calls:
            fn = tc["function"]["name"]
            args = parse_args(tc)
            tc_id = tc.get("id", "0")
            req_path = args.get("path", "/")

            if fn == "list_doc":
                saw_list = True
                content = json.dumps(_fake_for(req_path), ensure_ascii=False)
            elif fn == "read_doc":
                saw_read_path = req_path
                fake = _fake_for(req_path)
                # read は markdown 文字列、それ以外 (404) は dict をそのまま渡す
                content = (
                    fake
                    if isinstance(fake, str)
                    else json.dumps(fake, ensure_ascii=False)
                )
            else:
                content = "{}"

            messages.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "content": content,
            })

        if saw_read_path is not None:
            break

    assert saw_list, "list_doc が一度も呼ばれなかった"
    assert saw_read_path is not None, (
        "list_doc で関連 CQ が見えたのに read_doc が呼ばれなかった "
        "(lookup だけで終わった)"
    )
    # 関連 CQ の path で読まれていること(他のセクションを読んだら NG)
    assert saw_read_path.startswith("/肺癌_2024-非小細胞癌/CQ"), (
        f"expected read on a CQ under 非小細胞癌, got {saw_read_path!r}"
    )
