"""Unit tests for nucllei_client (httpx.MockTransport, no server needed).

Run: uv run --group dev --group evals pytest evals/harness/test_nucllei_client.py -q
"""

from __future__ import annotations

import json

import httpx
import pytest

from evals.harness.nucllei_client import AskResult, NucllieClient, extract_tool_calls

CHAT_ID = "chat-0001"


def _sse(frames: list[dict]) -> bytes:
    return b"".join(f"data: {json.dumps(f)}\n\n".encode() for f in frames)


def _envelope(message_id: str, etype: str, data: dict) -> dict:
    return {"chat_id": CHAT_ID, "message_id": message_id, "data": {"type": etype, "data": data}}


class FakeServer:
    """Minimal nucllei: chats/new -> chat/completions (SSE) -> chats/{id}."""

    def __init__(self, *, new_chat_5xx: int = 0, sse_error: bool = False):
        self.requests: list[httpx.Request] = []
        self.new_chat_5xx = new_chat_5xx
        self.sse_error = sse_error
        self.created: dict | None = None
        self.assistant_id: str | None = None
        self.stopped: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path

        if path == "/api/v1/chats/new":
            if self.new_chat_5xx > 0:
                self.new_chat_5xx -= 1
                return httpx.Response(503, text="upstream down")
            self.created = json.loads(request.content)
            self.assistant_id = self.created["chat"]["history"]["currentId"]
            return httpx.Response(200, json={"id": CHAT_ID, "chat": self.created["chat"]})

        if path == "/api/chat/completions":
            body = json.loads(request.content)
            mid = body["id"]
            frames = [
                _envelope(mid, "task", {"task_id": "task-42"}),
                _envelope(mid, "chat:completion", {"delta": {"block": "reasoning", "content": "考え中"}}),
                _envelope(mid, "chat:completion", {"delta": {"block": "text", "content": "答えは "}}),
                _envelope(mid, "chat:completion", {"delta": {"block": "text", "content": "2"}}),
                _envelope(mid, "chat:completion", {"content": "答えは 2", "content_blocks": []}),
                _envelope(mid, "chat:completion", {"usage": {"prompt_tokens": 10, "completion_tokens": 5}}),
            ]
            if self.sse_error:
                frames.append(_envelope(mid, "chat:completion", {"error": {"content": "boom"}, "done": True}))
            else:
                frames.append(_envelope(mid, "chat:completion", {"done": True, "content": "答えは 2"}))
            # frames can still arrive after done (done is not EOF)
            frames.append(_envelope(mid, "chat:completion", {"usage": {"prompt_tokens": 10, "completion_tokens": 5, "context_size": 262144}}))
            return httpx.Response(200, content=_sse(frames), headers={"content-type": "text/event-stream"})

        if path == f"/api/v1/chats/{CHAT_ID}" and request.method == "GET":
            mid = self.assistant_id
            assert mid is not None
            uid = self.created["chat"]["messages"][0]["id"]
            persisted = {
                "id": mid,
                "parentId": uid,
                "role": "assistant",
                "content": "答えは 2",
                "done": True,
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "context_size": 262144},
                "content_blocks": [
                    {"type": "reasoning", "content": "考え中", "done": True, "duration": 1},
                    {
                        "type": "tool_calls",
                        "calls": [
                            {"id": "call_1", "type": "function", "function": {"name": "run_python", "arguments": "{\"code\": \"print(1+1)\"}"}},
                            {"id": "call_2", "type": "function", "function": {"name": "doc_search", "arguments": ""}},
                        ],
                        "results": [
                            {"tool_call_id": "call_1", "content": "2\n" + "x" * 500},
                        ],
                    },
                    {"type": "text", "content": "答えは 2"},
                ],
            }
            if self.sse_error:
                persisted["error"] = {"content": "boom"}
            chat = {"history": {"currentId": mid, "messages": {uid: self.created["chat"]["messages"][0], mid: persisted}}}
            return httpx.Response(200, json={"id": CHAT_ID, "chat": chat})

        if path == f"/api/v1/chats/{CHAT_ID}" and request.method == "DELETE":
            return httpx.Response(200, json=True)

        if path.startswith("/api/tasks/stop/"):
            self.stopped.append(path.rsplit("/", 1)[-1])
            return httpx.Response(200, json={"status": True})

        if path == "/api/models":
            return httpx.Response(200, json={"data": [{"id": "kimi-k2", "name": "Kimi"}]})

        return httpx.Response(404, text=f"unexpected {request.method} {path}")


@pytest.fixture
def server():
    return FakeServer()


def _client(server: FakeServer) -> NucllieClient:
    return NucllieClient("http://nuc.test/", "sk-test", transport=httpx.MockTransport(server.handler))


def test_create_chat_body_shape(server):
    with _client(server) as c:
        c.ask("kimi-k2", "1+1?")

    new_req = next(r for r in server.requests if r.url.path == "/api/v1/chats/new")
    assert new_req.headers["authorization"] == "Bearer sk-test"
    chat = json.loads(new_req.content)["chat"]
    user, assistant = chat["messages"]
    assert user["role"] == "user" and user["content"] == "1+1?" and user["parentId"] is None
    assert assistant["role"] == "assistant" and assistant["content"] == ""
    assert assistant["parentId"] == user["id"]
    assert user["childrenIds"] == [assistant["id"]]
    assert chat["history"]["currentId"] == assistant["id"]
    assert set(chat["history"]["messages"]) == {user["id"], assistant["id"]}
    assert chat["models"] == ["kimi-k2"]

    comp_req = next(r for r in server.requests if r.url.path == "/api/chat/completions")
    body = json.loads(comp_req.content)
    assert body["chat_id"] == CHAT_ID
    assert body["id"] == assistant["id"]
    assert body["stream"] is True
    assert body["model"] == "kimi-k2"
    assert body["messages"] == [{"role": "user", "content": "1+1?"}]


def test_ask_result_extraction(server):
    with _client(server) as c:
        r = c.ask("kimi-k2", "1+1?")

    assert isinstance(r, AskResult)
    assert r.chat_id == CHAT_ID
    assert r.message_id == server.assistant_id
    assert r.task_id == "task-42"
    assert r.content == "答えは 2"
    assert r.reasoning == "考え中"
    assert r.error is None
    assert r.usage["context_size"] == 262144  # the usage frame after done is read as well
    assert r.ttft_ms is not None and r.ttrt_ms is not None and r.ttat_ms is not None
    assert r.ttft_ms <= r.ttat_ms
    assert r.total_ms is not None and r.total_ms >= r.ttat_ms

    assert [t["name"] for t in r.tool_calls] == ["run_python", "doc_search"]
    assert r.tool_calls[0]["arguments"] == {"code": "print(1+1)"}
    assert r.tool_calls[0]["result_preview"].startswith("2\n")
    assert len(r.tool_calls[0]["result_preview"]) == 200
    assert r.tool_calls[1]["arguments"] == {}
    assert r.tool_calls[1]["result_preview"] is None
    assert r.raw["content_blocks"][1]["type"] == "tool_calls"
    # stop is not called on normal completion
    assert server.stopped == []


def test_sse_error_terminal_sets_error_and_still_fetches_chat():
    server = FakeServer(sse_error=True)
    with _client(server) as c:
        r = c.ask("kimi-k2", "1+1?")
    assert r.error == "boom"
    assert r.content == "答えは 2"  # the chat is still fetched
    assert any(q.url.path == f"/api/v1/chats/{CHAT_ID}" for q in server.requests)


def test_create_chat_retries_on_5xx():
    server = FakeServer(new_chat_5xx=2)
    with _client(server) as c:
        r = c.ask("kimi-k2", "1+1?")
    assert r.chat_id == CHAT_ID
    assert sum(1 for q in server.requests if q.url.path == "/api/v1/chats/new") == 3


def test_create_chat_gives_up_after_3_5xx():
    server = FakeServer(new_chat_5xx=5)
    with _client(server) as c, pytest.raises(httpx.HTTPStatusError):
        c.ask("kimi-k2", "1+1?")
    assert sum(1 for q in server.requests if q.url.path == "/api/v1/chats/new") == 3


def test_interrupt_stops_task(server, monkeypatch):
    def boom(*a, **k):
        raise KeyboardInterrupt

    with _client(server) as c:
        # interrupt after task_id has been received
        original = c._apply_event

        def apply(envelope, result, *rest):
            original(envelope, result, *rest)
            if result.task_id:
                raise KeyboardInterrupt

        monkeypatch.setattr(c, "_apply_event", apply)
        with pytest.raises(KeyboardInterrupt):
            c.ask("kimi-k2", "1+1?")
    assert server.stopped == ["task-42"]


def test_list_models_and_delete_chat(server):
    with _client(server) as c:
        assert [m["id"] for m in c.list_models()] == ["kimi-k2"]
        c.delete_chat(CHAT_ID)
    assert server.requests[-1].method == "DELETE"


def test_extract_tool_calls_accepts_internal_content_key():
    blocks = [
        {
            "type": "tool_calls",
            "content": [{"id": "c", "type": "function", "function": {"name": "f", "arguments": "not json"}}],
            "results": [{"tool_call_id": "c", "content": {"k": 1}}],
        }
    ]
    [tc] = extract_tool_calls(blocks)
    assert tc["arguments"] == "not json"
    assert tc["result_preview"] == '{"k": 1}'
