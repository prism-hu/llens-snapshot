"""nucllei chat-wire client (for evaluation).

`client.py` hits SGLang directly (no tool execution). This one goes through nucllei's chat-wire
(`POST /api/v1/chats/new` -> `POST /api/chat/completions` with chat_id + assistant id), so
nucllei's native tool loop / skills / sandbox run on the server side.
The conversation also stays in the UI (can be removed with `delete_chat` after evaluation).

Primary spec: nucllei `README_API.md` §4.1 (auth) / §4.3 "Chat-wire mode" / §4.6.
The shape of the persisted message's `content_blocks` is based on nucllei `nucllei/utils/content_blocks.py`
(`snapshot_content_blocks`):
  {"type":"text","content"} / {"type":"reasoning","content","done","duration"}
  / {"type":"tool_calls","calls":[OpenAI-style {id,type,function{name,arguments}}],
     "results":[{"tool_call_id","content",...}]}
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)

RESULT_PREVIEW_CHARS = 200


@dataclass
class AskResult:
    chat_id: str
    message_id: str
    content: str = ""
    reasoning: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    ttft_ms: float | None = None  # first delta (reasoning or text, whichever first)
    ttat_ms: float | None = None  # first text (answer) delta
    ttrt_ms: float | None = None  # first reasoning delta
    total_ms: float | None = None  # POST completions → server closes SSE
    error: str | None = None
    task_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)  # persisted assistant message


class NucllieClient:
    """Synchronous client for the nucllei chat-wire API."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 900.0,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(timeout, connect=30.0),
            transport=transport,
        )

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> NucllieClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- public API ----------------------------------------------------------

    def list_models(self) -> list[dict[str, Any]]:
        """`GET /api/models` — list of workspace models (the id is the `model` for completions)."""
        r = self._client.get("/api/models")
        r.raise_for_status()
        body = r.json()
        return body.get("data", body) if isinstance(body, dict) else body

    def delete_chat(self, chat_id: str) -> None:
        r = self._client.delete(f"/api/v1/chats/{chat_id}")
        r.raise_for_status()

    def get_chat(self, chat_id: str) -> dict[str, Any]:
        """The `chat` body of `GET /api/v1/chats/{id}` (holds history.messages)."""
        r = self._client.get(f"/api/v1/chats/{chat_id}")
        r.raise_for_status()
        body = r.json()
        return body.get("chat", body)

    def get_executions(self, chat_id: str) -> dict[str, Any]:
        """`GET /api/v1/chats/{id}/executions` (§4.6) — rows of background executions."""
        r = self._client.get(f"/api/v1/chats/{chat_id}/executions")
        r.raise_for_status()
        return r.json()

    def stop_task(self, task_id: str) -> None:
        try:
            self._client.post(f"/api/tasks/stop/{task_id}", timeout=30.0)
        except httpx.HTTPError as e:  # best effort
            log.warning("stop task %s failed: %s", task_id, e)

    def ask(
        self,
        model: str,
        prompt: str,
        *,
        title: str | None = None,
        files: list[dict[str, Any]] | None = None,
    ) -> AskResult:
        """Run one round trip over chat-wire and return the persisted assistant message."""
        chat_id, user_id, assistant_id = self.create_chat(model, prompt, title=title, files=files)
        result = AskResult(chat_id=chat_id, message_id=assistant_id)

        body: dict[str, Any] = {
            "model": model,
            "stream": True,
            "chat_id": chat_id,
            "id": assistant_id,
            "messages": [{"role": "user", "content": prompt}],
        }
        if files:
            body["files"] = files

        streamed_text: list[str] = []
        streamed_reasoning: list[str] = []
        finished = False
        t0 = time.perf_counter()
        try:
            with self._client.stream("POST", "/api/chat/completions", json=body) as response:
                response.raise_for_status()
                for envelope in _iter_sse(response):
                    self._apply_event(envelope, result, t0, streamed_text, streamed_reasoning)
            finished = True
        finally:
            result.total_ms = (time.perf_counter() - t0) * 1000
            if not finished and result.task_id:
                # Interrupted (KeyboardInterrupt / timeout): the server-side task does not
                # stop on disconnect (§4.3), so stop it explicitly
                self.stop_task(result.task_id)

        self._fill_from_chat(result, streamed_text, streamed_reasoning)
        return result

    # -- chat creation -------------------------------------------------------

    def create_chat(
        self,
        model: str,
        prompt: str,
        *,
        title: str | None = None,
        files: list[dict[str, Any]] | None = None,
    ) -> tuple[str, str, str]:
        """`POST /api/v1/chats/new` (§4.3). Retries up to 3 times on 5xx.

        Returns (chat_id, user_message_id, assistant_message_id).
        """
        user_id = str(uuid.uuid4())
        assistant_id = str(uuid.uuid4())
        now = int(time.time())
        user_msg: dict[str, Any] = {
            "id": user_id,
            "parentId": None,
            "childrenIds": [assistant_id],
            "role": "user",
            "content": prompt,
            "timestamp": now,
            "models": [model],
        }
        if files:
            user_msg["files"] = files
        assistant_msg: dict[str, Any] = {
            "id": assistant_id,
            "parentId": user_id,
            "childrenIds": [],
            "role": "assistant",
            "content": "",
            "model": model,
            "modelName": model,
            "timestamp": now,
        }
        chat: dict[str, Any] = {
            "title": title or prompt[:50],
            "models": [model],
            "messages": [user_msg, assistant_msg],
            "history": {
                "currentId": assistant_id,
                "messages": {user_id: user_msg, assistant_id: assistant_msg},
            },
        }

        last_exc: Exception | None = None
        for attempt in range(3):
            r = self._client.post("/api/v1/chats/new", json={"chat": chat})
            if r.status_code < 500:
                r.raise_for_status()
                return r.json()["id"], user_id, assistant_id
            last_exc = httpx.HTTPStatusError(
                f"chats/new returned {r.status_code}", request=r.request, response=r
            )
            log.warning("chats/new %s (attempt %d/3)", r.status_code, attempt + 1)
            time.sleep(0.5 * (attempt + 1))
        assert last_exc is not None
        raise last_exc

    # -- internals -----------------------------------------------------------

    @staticmethod
    def _apply_event(
        envelope: dict[str, Any],
        result: AskResult,
        t0: float,
        streamed_text: list[str],
        streamed_reasoning: list[str],
    ) -> None:
        inner = envelope.get("data")
        if not isinstance(inner, dict):
            return
        etype = inner.get("type")
        data = inner.get("data")
        if etype == "task":
            if isinstance(data, dict) and data.get("task_id"):
                result.task_id = data["task_id"]
            return
        if etype not in (None, "chat:completion") or not isinstance(data, dict):
            return

        now_ms = (time.perf_counter() - t0) * 1000
        delta = data.get("delta")
        if isinstance(delta, dict) and delta.get("content"):
            block = delta.get("block", "text")
            if result.ttft_ms is None:
                result.ttft_ms = now_ms
            if block == "reasoning":
                if result.ttrt_ms is None:
                    result.ttrt_ms = now_ms
                streamed_reasoning.append(delta["content"])
            else:
                if result.ttat_ms is None:
                    result.ttat_ms = now_ms
                streamed_text.append(delta["content"])

        if data.get("usage"):
            result.usage = data["usage"]

        if data.get("error"):
            result.error = _error_text(data["error"])

    def _fill_from_chat(
        self, result: AskResult, streamed_text: list[str], streamed_reasoning: list[str]
    ) -> None:
        msg: dict[str, Any] = {}
        try:
            chat = self.get_chat(result.chat_id)
            msg = (chat.get("history") or {}).get("messages", {}).get(result.message_id) or {}
        except httpx.HTTPError as e:
            log.warning("fetch chat %s failed: %s", result.chat_id, e)
            if result.error is None:
                result.error = f"fetch chat failed: {e}"

        result.raw = msg
        blocks = msg.get("content_blocks") or []
        text_blocks = [b for b in blocks if b.get("type") == "text"]
        reasoning_blocks = [b for b in blocks if b.get("type") == "reasoning"]

        result.content = msg.get("content") or "\n\n".join(
            b.get("content", "") for b in text_blocks if b.get("content")
        ) or "".join(streamed_text)
        result.reasoning = "\n\n".join(
            b.get("content", "") for b in reasoning_blocks if b.get("content")
        ) or "".join(streamed_reasoning)
        result.tool_calls = extract_tool_calls(blocks)
        if msg.get("usage") and not result.usage:
            result.usage = msg["usage"]
        if result.error is None and msg.get("error"):
            result.error = _error_text(msg["error"])


def extract_tool_calls(content_blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract {name, arguments, result_preview} from persisted content_blocks (best effort)."""
    out: list[dict[str, Any]] = []
    for block in content_blocks:
        if block.get("type") != "tool_calls":
            continue
        calls = block.get("calls") or block.get("content") or []
        results = {r.get("tool_call_id"): r for r in block.get("results") or []}
        for call in calls:
            fn = call.get("function") or {}
            args_raw = fn.get("arguments")
            arguments: Any = args_raw
            if isinstance(args_raw, str):
                try:
                    arguments = json.loads(args_raw) if args_raw.strip() else {}
                except json.JSONDecodeError:
                    arguments = args_raw
            res = results.get(call.get("id"))
            preview = None
            if res is not None:
                content = res.get("content")
                if not isinstance(content, str):
                    content = json.dumps(content, ensure_ascii=False)
                preview = content[:RESULT_PREVIEW_CHARS]
            out.append(
                {
                    "id": call.get("id"),
                    "name": fn.get("name"),
                    "arguments": arguments,
                    "result_preview": preview,
                }
            )
    return out


def _error_text(error: Any) -> str:
    if isinstance(error, dict):
        return str(error.get("content") or error.get("message") or error)
    return str(error)


def _iter_sse(response: httpx.Response):
    """Yield `data: {...}` frames as dicts. Reads until the server closes."""
    for line in response.iter_lines():
        if not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            yield json.loads(payload)
        except json.JSONDecodeError:
            log.debug("skip non-JSON SSE frame: %r", payload[:120])


def _main() -> None:
    import argparse
    import os

    parser = argparse.ArgumentParser(description="Smoke test for nucllei chat-wire client.")
    parser.add_argument("--base-url", default=os.environ.get("EVAL_NUCLLEI_ORIGIN", "https://llens.prism-hu.org"))
    parser.add_argument("--api-key", default=os.environ.get("EVAL_NUCLLEI_API_KEY", ""))
    parser.add_argument("--model", required=True)
    parser.add_argument("--prompt", default="日本の首都はどこですか。一文で答えて。")
    parser.add_argument("--keep", action="store_true", help="do not delete the chat after evaluation")
    args = parser.parse_args()

    def fmt(x: float | None) -> str:
        return f"{x:.0f}ms" if x is not None else "-"

    with NucllieClient(args.base_url, args.api_key) as c:
        r = c.ask(args.model, args.prompt)
        print(f"chat_id : {r.chat_id}")
        print(f"TTFT    : {fmt(r.ttft_ms)}  TTAT: {fmt(r.ttat_ms)}  total: {fmt(r.total_ms)}")
        print(f"usage   : {r.usage}")
        print(f"error   : {r.error}")
        for tc in r.tool_calls:
            print(f"tool    : {tc['name']}({tc['arguments']}) -> {tc['result_preview']!r}")
        if r.reasoning:
            print(f"\n--- reasoning (head) ---\n{r.reasoning[:300]}")
        print(f"\n--- answer ---\n{r.content}")
        if not args.keep:
            c.delete_chat(r.chat_id)


if __name__ == "__main__":
    _main()
