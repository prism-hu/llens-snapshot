"""
title: Doc
description: doc ツール。収録文書ツリーの目次 (list_doc), 本文 (read_doc), 全文検索 (grep_doc)
"""

import logging
import os
from typing import Any, Dict, Optional
from urllib.parse import unquote

import requests
from pydantic import BaseModel, Field

from nucllei.utils.tool_errors import ToolCallError, ToolError, ToolFailure, ToolFatal, ToolOutcome
from nucllei.utils.tool_render import Markdown

logger = logging.getLogger(__name__)

# Environment differences are absorbed by the nucllei container env (per-environment overrides in compose apply as-is).
# Resolving at import time and passing to Field(default=) is because with default_factory the default
# drops out of the valve's JSON schema, the admin UI shows an empty field, and "where it currently points"
# cannot be read (measured: default_factory items have default=None in the spec).
# Conversely, valves carrying secrets should not appear in the schema, so keep those as default_factory
# (no such case in the current tools — follow this rule when adding a valve that holds a secret).
CAGE_API_BASE = os.getenv("CAGE_API_BASE", "http://host.docker.internal:13303")

# A doc's address is a single string (<redacted> 2026-10-06): **path = sandbox path = page URL = citation**.
# Leaf `/ext/doc/<category>/…/<name>.md`, directory `/ext/doc/<category>/…/` (trailing `/`).
# cage returns the canonical form (`path` of each list / grep entry; read via the header X-Doc-Path =
# percent-encoded), and input is accepted as the same node regardless of prefix, `.md` or trailing `/`.
# From 2026-10-05 to 06 the page URL (`/ext/cage/doc/…`) was returned as `url` separately from path (`/<category>/…`),
# but since the address was unified into one, there is no `url` (duplication wastes tokens).
# read_doc prepends one line `path: <canonical>` to the body (so the citation can be written in canonical
# form even when called with the old form / without .md. Kept in Japanese, no brackets). The earlier "`出典: [<url>]` at the end" was
# dropped because the model mimicked the line's shape and wrote citation-only lines (staging 2026-10-06). Show it as a value and
# leave the model only the one step of wrapping it in `[` `]`. Not added for nodes with an empty body (nothing to cite).
#
# Figure / original URLs (`/ext/doc-static/…`, same-server relative URLs = sandbox paths) are returned as-is from
# the cage-generated body. The origin is not filled in (so it does not break when the entry origin changes).

# Failures raise ToolError (nucllei #145, README_API §5.1 "Failures"). nucllei composes the text as
# `ERROR: {fact}。次の手: {next}`. fact = a statement of fact (may be relayed to the user as-is),
# next = an instruction only for the model. Plain style, no internal names (env, cage code, host).
# kind: path / node missing (404 not_found) = outcome / argument shape (bad_path, static_path, bad_regex, bad_query, 422) = call /
# unreachable, timeout, other 4xx / 5xx = failure /
# broken on our side (401/403 = auth / upstream config, 2xx body not parseable as JSON = deployment mismatch) = fatal.
# cage's 4xx is JSON {error, message | missing/valid_path/siblings_at_parent}. message may be
# developer-facing, so only pick up what lets the model fix the call (missing / valid_path / children list).
# The HTTP exit and failure shaping share the same shape as monitoring.py (`_post` / `_error_info` / `_http_error` /
# `_json`). A tool is one DB row = one module and cannot have a shared module, so the shape is duplicated.
_RETRY = "少し待って 1 回だけ再試行し、だめならこの事実をユーザーに伝える。"
_UNREACHABLE = "収録文書のサーバーに接続できない"


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
    if code == "not_found":
        valid = info.get("valid_path") or "/ext/doc/"
        siblings = info.get("siblings_at_parent") or []
        return ToolOutcome(
            f"その path は収録文書に無い。見つからない階層名: {info.get('missing')}。"
            f"有効なのは {valid} まで。その階層の子: {', '.join(siblings) if siblings else '(なし)'}",
            f"{valid} に子の名前を繋いだ path で呼び直すか、list_doc({valid}) で目次を取り直す。",
        )
    if code == "bad_path":
        return ToolCallError(
            f"doc の path が / 始まりの文字列でない (指定: {payload.get('path')})",
            "list_doc / grep_doc が返した `/ext/doc/…` の path で呼び直す。",
        )
    if code == "static_path":
        return ToolCallError(
            f"{payload.get('path')} は図・原本のファイルで、doc のノードではない",
            "その本文は図・原本を載せたノードの path で read_doc する。ページを見るなら feed_image(url=) にそのまま渡す。",
        )
    if code == "bad_regex":
        # Do not show Python re's English message (keep it in the log)
        logger.info(f"[doc] bad_regex: {info.get('message')!r}")
        return ToolCallError("query が正規表現として読めない", "書き直すか regex=false で探す。")
    if code == "bad_query":
        return ToolCallError("query が空", "探す語を渡す。")
    if r.status_code == 422:
        return ToolCallError(
            "引数の型か範囲が違う (limit は 1〜50、offset は 0 以上)", "直して呼び直す。"
        )
    if r.status_code in (401, 403):
        # doc has no auth — a rejection is a deployment error (upstream proxy / config) that waiting will not fix
        logger.error(f"[doc] auth rejected {r.status_code} {code}")
        return ToolFatal("収録文書のサーバーがエラーを返した", _RETRY)
    logger.warning(f"[doc] server error {r.status_code} {code}")
    return ToolFailure("収録文書のサーバーがエラーを返した", _RETRY)


def _json(r: Any) -> Any:
    """JSON body of a 2xx. If unparseable, ToolFatal (deployment mismatch / relay anomaly)."""
    try:
        return r.json()
    except ValueError:
        logger.warning(f"[doc] response is not JSON: {r.text[:200]!r}")
        raise ToolFatal("収録文書のサーバーの応答を解釈できない", _RETRY)


class Tools:
    # Table of contents (list_doc) / body (read_doc) / full-text search (grep_doc) of the document tree.
    # A thin HTTP client that hits the cage API (`POST /doc/list` / `POST /doc/read` /
    # `POST /doc/grep`).
    # NOTE: nucllei shows the class docstring to the model as the toolkit-wide note
    #   (2026-08-16) — this dev-facing description is kept in comments. If a model-facing note
    #   common to the 3 functions becomes necessary, write it as a docstring.

    class Valves(BaseModel):
        api_base: str = Field(
            default=CAGE_API_BASE,
            description="cage API server base URL。既定は nucllei の env (CAGE_API_BASE) を流用し、"
            "無ければ host.docker.internal:13303 (compose の extra_hosts で host を指す)。"
            "cage は host の uv run cage で動く (compose の service ではない)",
        )
        timeout_sec: float = Field(
            default=15.0,
            description="HTTP request timeout in seconds",
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
            logger.warning(f"[doc] {route} unreachable: {e!r}")
            raise ToolFailure(_UNREACHABLE, _RETRY)
        if r.status_code >= 400:
            raise _http_error(r, payload)
        return r

    # ----- functions called by the model -----

    def list_doc(self, path: str = "/ext/doc/") -> Dict[str, Any]:
        """指定 path の目次 (children 一覧) を取得する。

        doc の path は sandbox のパスでもあり、ユーザーがそのまま開けるページの URL でもある
        (リーフ `/ext/doc/…/<名前>.md`、ディレクトリ `/ext/doc/…/` で末尾 `/`)。
        `body_chars` は read_doc で返る本文の文字数 (0 = 本文なし)。中間ノードでも本文を持つことがある。

        :param path: doc の path (`/ext/doc/…`。sandbox のパス・ページの URL と同じ文字列。リーフは `….md`、ディレクトリは末尾 `/`)。省略時は root (`/ext/doc/`)。
        :return: {path, title, meta, body_chars, child_count,
                  children: [{path, title, body_chars, child_count}]}
                 `body_chars` は read_doc で返る本文の文字数 (0 = 本文なし)。
                 read 自体はどのノードでも呼んでよい (中間ノードでも本文を持つ場合がある)。
                 読む前にペイロードの大きさが分かるので、長大ノードを read_doc する前の
                 判断材料に使う。
                 失敗は ToolError を raise (path 誤りは outcome で、見つからない階層名・
                 有効な path・その階層の子を含む)。
        """
        return _json(self._post("/doc/list", {"path": path}))

    def read_doc(self, path: str) -> Markdown:
        """指定 path の本文 (Markdown) を取得する。

        list_doc / grep_doc が返した path をそのまま渡す (両者は同じノードに対する別操作)。
        本文を持たないノードに呼んだ場合は空文字列が返る (エラーにはしない)。
        本文の先頭に `path: <このノードの path>` の 1 行が付く (本文の一部ではない)。

        本文中の図・原本の URL は同一サーバの相対 URL `/ext/…`。本文にある literal のまま
        本文に貼る (`![図: …](/ext/…)`) / `feed_image(url=)` に渡す (ホストを補わない・組み替えない)。
        本文のリンク (`原本 PDF: [ラベル](URL)`・様式や添付の `[ラベル](/ext/…)` 等) を回答に出すときは
        markdown のリンク全体をラベルごと 1 文字も変えずにコピーする (URL・ファイル名を打ち直さない、ラベルを言い換えない)。

        出典: 先頭の `path:` 行の path をそのまま `[ ]` で囲み、それに基づく主張の直後に置く
        (番号と一覧は画面が作る)。箇所は path の末尾に `#見出し` (本文の `#` 見出し行) か
        `#:~:text=原文の一節` を 1 つ付けて示せる。`原本 PDF:` 等の本文のリンクは出典ではない。

        :param path: doc の path (`/ext/doc/…`。sandbox のパス・ページの URL と同じ文字列。リーフは `….md`、ディレクトリは末尾 `/`)。
        :return: 先頭の `path:` 行 + 本文 Markdown 文字列 (空ノードは "")。
                 失敗は ToolError を raise (path 誤りは outcome で、見つからない階層名・
                 有効な path・その階層の子を含む)。
        """
        r = self._post("/doc/read", {"path": path})
        body = r.text
        addr = unquote(r.headers.get("x-doc-path", ""))
        if not body or not addr:
            return body
        return f"path: {addr}\n\n{body}"

    def grep_doc(
        self,
        query: str,
        path: str = "/ext/doc/",
        regex: bool = False,
        offset: int = 0,
        limit: int = 10,
    ) -> Dict[str, Any]:
        """指定 path の subtree 全体を query で全文検索する。

        各ノードの title / description (front matter の description) / body を独立に照合し、
        いずれかに当たったノードを 1 entry として**関連度の降順** (先頭ほど query の主題そのもの)
        で返す。case-insensitive + NFKC 正規化 (substring 時) で全角/半角・大文字小文字の揺れは
        吸収される。スニペットは body の最初の 1 箇所 (±40字) だけで、本文は read_doc で引く。

        query は `regex=False` なら substring 一致、`regex=True` なら Python の
        `re.compile(query, re.IGNORECASE)`。path が root (`/ext/doc/`) なら全カテゴリ。
        `has_more` が true なら `next_offset` を offset に渡すと続きが返る。
        応答直下の `match_count` は当たったノードの総数 (この応答の件数は `returned`)。

        応答の `mode` が**どうやって探したか**を名乗る (切替引数は無い。自動):
        - `"exact"` — query の字面がそのまま照合された (通常)。regex=True のとき、query が
          1 語にしか割れなかったときも exact (= 語分割は試していない)
        - `"loose"` — **字面一致が 0 件**だったので query を語に分割して探し直した結果。
          `terms` (割った語) / `absent_terms` (この範囲に 1 件も出てこなかった語) /
          `ignored_terms` (「対応」「防止」のようにありふれすぎて外した語) / `note` (事実の文 +「次の手:」) が
          付き、各 entry に `matched_terms` (そのノードで当たった語) が付く。entry の `match_count` は
          語ごとの出現回数の合計に変わる。**loose の結果は query が本文に字面通り書いてある
          ことを意味しない** — read_doc で本文を確認するまで引用しない。`absent_terms` /
          `ignored_terms` があるときは `note` の「次の手:」に従う

        :param query: 検索文字列 (substring、regex=True なら正規表現)。
        :param path: 検索範囲を限定する doc の path (`/ext/doc/…`。sandbox のパス・ページの URL と同じ文字列。ディレクトリは末尾 `/`)。省略時は root (`/ext/doc/` = 全体)。
        :param regex: True で正規表現マッチに切り替える。デフォルト False。
        :param offset: 何件目から返すか (0 起点)。続きを取るときに `next_offset` を渡す。
        :param limit: 返す件数 (1〜50、既定 10)。
        :return: {path, query, regex, mode, match_count, offset, returned,
                  has_more, next_offset?, matches: [entry...]}
                 `match_count` はマッチしたノードの**総数** (この応答の件数は `returned`)。
                 loose のときだけ terms / absent_terms / ignored_terms / note が付く。
                 entry: {path (出典にするのは read_doc で読んでから), title,
                         body_chars (0 = 本文なし), child_count,
                         match_count: {title: 0|1, description: 0|1, body: int},
                         description? (description ヒット時のみ、全文),
                         body_first_match_snippet? (body ヒット時のみ),
                         matched_terms? (loose のときだけ)}
                 一致 0 件は失敗ではない (matches が空の正常な結果。loose の note が次の手を持つ)。
                 失敗は ToolError を raise (path 誤りは outcome で、見つからない階層名・
                 有効な path・その階層の子を含む)。
        """
        payload: Dict[str, Any] = {
            "query": query,
            "path": path,
            "regex": regex,
            "offset": offset,
            "limit": limit,
        }
        return _json(self._post("/doc/grep", payload))
