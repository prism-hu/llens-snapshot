import json
from pathlib import Path

from evals.tasks.docqa.dump_chats import dump_chats, load_rows


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def get_chat(self, chat_id: str) -> dict:
        self.calls.append(("chat", chat_id))
        return {"id": chat_id, "history": {"messages": {}}}

    def get_executions(self, chat_id: str) -> dict:
        self.calls.append(("exec", chat_id))
        return {"items": []}


def _write_results(p: Path, rows: list[dict]) -> None:
    p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def test_dump_writes_skips_and_forces(tmp_path: Path) -> None:
    res = tmp_path / "r.jsonl"
    _write_results(res, [
        {"id": "a-001", "chat_id": "c1"},
        {"id": "a-002", "chat_id": None},
        {"id": "a-003", "chat_id": "c3"},
    ])
    rows = load_rows([res])
    out = tmp_path / "chats"
    c = FakeClient()

    st = dump_chats(c, rows, out)
    assert (st.rows, st.no_chat_id, st.written, st.skipped, st.failed) == (3, 1, 2, 0, 0)
    assert json.loads((out / "a-001.json").read_text())["id"] == "c1"
    assert sorted(p.name for p in out.iterdir()) == ["a-001.json", "a-003.json"]

    st = dump_chats(c, rows, out)
    assert (st.written, st.skipped) == (0, 2)

    st = dump_chats(c, rows, out, executions=True)
    assert (st.written, st.executions_written, st.skipped) == (0, 2, 0)
    assert (out / "a-001.executions.json").exists()

    st = dump_chats(c, rows, out, force=True, executions=True)
    assert (st.written, st.executions_written) == (2, 2)
