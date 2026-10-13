"""Tool integration tests.

tool 本体はモノレポ側 `nucllei/tools/doc.py` にある (nucllei 投入物として管理)。
cage パッケージ外なので importlib でファイルパスから直接ロードする。

`Tools` は本来 `requests.post` で cage API サーバと通信する。
テストでは `requests.post` を monkey-patch して、FastAPI の TestClient を
通してインプロセスで API を叩く形にする(本物のサーバ起動は不要)。

tool は nucllei を素で import する (失敗は `nucllei.utils.tool_errors.ToolError` を raise)。
cage の venv に nucllei は無いので、nucllei/tests の stub パッケージを sys.path に足して読む
(nucllei/tests/_harness.py と同じ。stub の tool_errors は nucllei の実ファイルの写し)。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from cage.server import app

_REPO = Path(__file__).resolve().parents[2]
_STUBS = str(_REPO / "nucllei" / "tests" / "stubs")
_CAGE_TOOL_PATH = _REPO / "nucllei" / "tools" / "doc.py"

# stub は doc.py を読み込む間だけ sys.path に置く (cage の他のテストに stub の markitdown 等を見せない)。
# 読み込んだ nucllei.* は sys.modules に残るので、後で import しても同じ物が返る。
sys.path.insert(0, _STUBS)
try:
    from nucllei.utils.tool_errors import ToolCallError, ToolOutcome

    _spec = importlib.util.spec_from_file_location("cage_tool", _CAGE_TOOL_PATH)
    cage_tool = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(cage_tool)
finally:
    sys.path.remove(_STUBS)
Tools = cage_tool.Tools


@pytest.fixture(scope="module")
def test_client():
    """Module-scoped TestClient(コンテキストマネージャでlifespan発火)。"""
    with TestClient(app) as c:
        yield c


@pytest.fixture
def tools(monkeypatch, test_client):
    """`requests.post` を TestClient に差し替えた Tools インスタンス。"""

    def fake_post(url, json=None, **kwargs):
        # TestClient は timeout 引数を受け付けない(httpx のテスト transport)。
        # 実環境では requests に渡るが、テストでは捨てる。
        kwargs.pop("timeout", None)
        path = urlparse(url).path
        return test_client.post(path, json=json, **kwargs)

    monkeypatch.setattr(cage_tool.requests, "post", fake_post)
    return Tools()


# ---------------------------------------------------------------------------
# lookup
# ---------------------------------------------------------------------------


def test_lookup_root_returns_dict(tools: Tools) -> None:
    result = tools.list_doc()
    assert isinstance(result, dict)
    assert result["path"] == "/ext/doc/"
    assert result["title"] == "知識ベース"


def test_lookup_default_path_is_root(tools: Tools) -> None:
    """path 省略 と path='/' が同じ結果を返す。"""
    a = tools.list_doc()
    b = tools.list_doc("/")
    assert a == b


def test_lookup_root_lists_categories(tools: Tools) -> None:
    """root の children は `/診療ガイドライン` などのカテゴリ階層。"""
    result = tools.list_doc()
    section_paths = [c["path"] for c in result["children"]]
    assert "/ext/doc/診療ガイドライン/" in section_paths


def test_lookup_category_lists_sections(tools: Tools) -> None:
    """カテゴリ直下は `<主題><版>_<小カテゴリ>` のセクション群。"""
    result = tools.list_doc("/ext/doc/診療ガイドライン/")
    section_paths = [c["path"] for c in result["children"]]
    assert "/ext/doc/診療ガイドライン/肺癌2025_非小細胞癌/" in section_paths
    assert "/ext/doc/診療ガイドライン/肺癌2025_小細胞癌/" in section_paths
    assert "/ext/doc/診療ガイドライン/中皮腫2025_治療/" in section_paths


def test_lookup_section_lists_cqs(tools: Tools) -> None:
    result = tools.list_doc("/ext/doc/診療ガイドライン/肺癌2025_非小細胞癌/")
    cq_paths = sorted(c["path"] for c in result["children"])
    assert "/ext/doc/診療ガイドライン/肺癌2025_非小細胞癌/CQ47.md" in cq_paths
    assert all(
        p.startswith("/ext/doc/診療ガイドライン/肺癌2025_非小細胞癌/CQ") for p in cq_paths
    )
    assert len(cq_paths) >= 8


def test_lookup_section_meta_includes_guideline(tools: Tools) -> None:
    """セクション meta に domain 由来の guideline / publisher が継承される。"""
    result = tools.list_doc("/ext/doc/診療ガイドライン/肺癌2025_非小細胞癌/")
    assert "肺癌診療ガイドライン" in result["meta"]["guideline"]
    assert result["meta"]["publisher"] == "日本肺癌学会"


def test_lookup_children_are_one_layer_only(tools: Tools) -> None:
    """children の各要素は path/title/body_chars/child_count のみ。さらに下を見るには再度 list。"""
    result = tools.list_doc("/")
    for c in result["children"]:
        assert set(c.keys()) == {"path", "title", "body_chars", "child_count"}


def test_lookup_response_never_contains_body(tools: Tools) -> None:
    """body は lookup の戻り値に絶対含めない (body_chars で文字数だけ)。"""
    result = tools.list_doc("/ext/doc/診療ガイドライン/肺癌2025_非小細胞癌/CQ47.md")
    assert "body" not in result
    assert "body_markdown" not in result
    assert result["body_chars"] > 0


def test_lookup_returns_child_count(tools: Tools) -> None:
    """child_count で「掘り下げる必要があるか」が判別できる。"""
    # カテゴリから見ると各セクションは child_count > 0
    cat = tools.list_doc("/ext/doc/診療ガイドライン/")
    section = next(
        c
        for c in cat["children"]
        if c["path"] == "/ext/doc/診療ガイドライン/肺癌2025_非小細胞癌/"
    )
    assert section["child_count"] >= 1

    # 直接 lookup すれば children が中身を持つ
    direct = tools.list_doc("/ext/doc/診療ガイドライン/肺癌2025_非小細胞癌/")
    assert len(direct["children"]) == direct["child_count"]
    assert direct["child_count"] >= 8

    # リーフは child_count=0
    leaf = tools.list_doc("/ext/doc/診療ガイドライン/肺癌2025_非小細胞癌/CQ47.md")
    assert leaf["child_count"] == 0


def test_lookup_body_chars_signal(tools: Tools) -> None:
    """`body_chars` で「ここで read_doc を呼ぶ意味があるか」と本文サイズを伝える (0 = 本文なし)。

    - root (`data/doc/_index.yaml`) → 0
    - 中間カテゴリ (`診療ガイドライン/_index.md`) → > 0 (license 本文を持つ)
    - セクション (`_index.md`) → > 0
    - CQ リーフ → > 0
    """
    assert tools.list_doc("/")["body_chars"] == 0
    assert tools.list_doc("/ext/doc/診療ガイドライン/")["body_chars"] > 0
    assert tools.list_doc("/ext/doc/診療ガイドライン/肺癌2025_非小細胞癌/")["body_chars"] > 0
    assert tools.list_doc("/ext/doc/診療ガイドライン/肺癌2025_非小細胞癌/CQ47.md")["body_chars"] > 0


def _error_text(call, cls=ToolOutcome) -> str:
    """call が cls を raise することを見て、モデルに見える全文 (`ERROR: 事実。次の手: …`) を返す。"""
    with pytest.raises(cls) as info:
        call()
    return info.value.text


def test_lookup_not_found_raises_an_outcome(tools: Tools) -> None:
    """存在しない path は ToolOutcome を raise する (AI が自己修正できる材料を日本語の文で載せる)。"""
    result = _error_text(lambda: tools.list_doc("/ext/doc/存在しない.md"))
    assert result.startswith("ERROR: その path は収録文書に無い。")
    assert "見つからない階層名: 存在しない。" in result
    assert "有効なのは /ext/doc/ まで。" in result and "診療ガイドライン/" in result
    assert (
        "次の手: /ext/doc/ に子の名前を繋いだ path で呼び直すか、list_doc(/ext/doc/) で目次を取り直す。"
        in result
    )


def test_lookup_not_found_in_middle_returns_correct_siblings(tools: Tools) -> None:
    """path の中段が間違っていたら、その地点で valid な siblings を返す。"""
    result = _error_text(lambda: tools.list_doc("/ext/doc/診療ガイドライン/肺癌2025_非小細胞癌/typoCQ.md"))
    assert "見つからない階層名: typoCQ。" in result
    assert "有効なのは /ext/doc/診療ガイドライン/肺癌2025_非小細胞癌/ まで。" in result
    assert "CQ47.md" in result  # valid_path に繋げば正規形になる名前


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------


def test_read_returns_markdown_string(tools: Tools) -> None:
    text = tools.read_doc("/ext/doc/診療ガイドライン/肺癌2025_非小細胞癌/CQ47.md")
    assert isinstance(text, str)
    assert "FLAURA" in text
    assert "## 推奨" in text
    assert "## 参考文献" in text


def test_read_starts_with_path_line(tools: Tools) -> None:
    """本文の先頭に `path: <正規形の path>` (日本語のまま・角括弧なし、list_doc の path と同じ値) が付き、
    末尾に `出典:` 行は付かない (モデルが行の形ごと真似て出典だけの行を書いたため廃止)。
    旧形・`.md` 無しで呼んでも先頭行は正規形 (出典は正規形で書けるように)。"""
    canon = "/ext/doc/診療ガイドライン/肺癌2025_非小細胞癌/CQ47.md"
    for given in (canon, canon[: -len(".md")], canon[len("/ext/doc"):], canon[len("/ext/doc"): -len(".md")]):
        text = tools.read_doc(given)
        lines = text.splitlines()
        assert lines[0] == f"path: {canon}", given
        assert lines[1] == ""
        assert "出典: [" not in text
    assert tools.list_doc(canon)["path"] == canon
    assert "url" not in tools.list_doc(canon)


def test_read_section_with_body(tools: Tools) -> None:
    """セクション (`_index.md`) の本文も取れる。"""
    text = tools.read_doc("/ext/doc/診療ガイドライン/肺癌2025_小細胞癌/")
    assert "小細胞肺癌" in text or "SCLC" in text


def test_read_root_returns_empty(tools: Tools) -> None:
    """data/doc/_index.yaml は本文なし → 空文字列。"""
    text = tools.read_doc("/")
    assert text == ""


def test_read_node_with_no_body_returns_empty(tools: Tools) -> None:
    """本文なしノード (root `data/doc/_index.yaml`) への read_doc は空文字列 (エラーにしない)。

    `body_chars == 0` のノードに read_doc を呼んでも安全に空文字が返ることを保証。
    (診療ガイドライン category は license 本文を持つため、root を bodyless ノードとして使う)
    """
    text = tools.read_doc("/")
    assert text == ""


def test_read_not_found_raises_an_outcome(tools: Tools) -> None:
    """read で 404 が起きたら、人間/AI が読めるエラー文の ToolOutcome を raise する。"""
    text = _error_text(lambda: tools.read_doc("/ext/doc/存在しない.md"))
    assert text.startswith("ERROR: その path は収録文書に無い。")
    assert "存在しない" in text  # missing segment
    assert "診療ガイドライン" in text  # available siblings


def test_read_not_found_includes_recovery_hint(tools: Tools) -> None:
    """エラー文に「list_doc で取り直せ」の指示が含まれている。"""
    text = _error_text(lambda: tools.read_doc("/ext/doc/診療ガイドライン/肺癌2025_非小細胞癌/typoCQ.md"))
    assert "ERROR" in text
    assert "typoCQ" in text
    assert "CQ47" in text  # available sibling
    assert "list_doc" in text  # recovery hint


# ---------------------------------------------------------------------------
# integration: 想定 AI シナリオ
# ---------------------------------------------------------------------------


def test_full_navigation_scenario(tools: Tools) -> None:
    """AI が EGFR 1次治療について調べる流れを検証 (lookup → lookup → lookup → read)。"""
    # 1. ルートでカテゴリ一覧
    root = tools.list_doc()
    assert "/ext/doc/診療ガイドライン/" in [c["path"] for c in root["children"]]

    # 2. カテゴリを開いてガイドライン/小カテゴリ一覧
    cat = tools.list_doc("/ext/doc/診療ガイドライン/")
    section_paths = [c["path"] for c in cat["children"]]
    assert "/ext/doc/診療ガイドライン/肺癌2025_非小細胞癌/" in section_paths

    # 3. セクションを開いて CQ 一覧 → CQ46 を選ぶ
    section = tools.list_doc("/ext/doc/診療ガイドライン/肺癌2025_非小細胞癌/")
    target = next(
        c
        for c in section["children"]
        if c["path"] == "/ext/doc/診療ガイドライン/肺癌2025_非小細胞癌/CQ47.md"
    )
    assert "EGFR陽性" in target["title"]
    assert "PS 0-1" in target["title"]
    assert target["body_chars"] > 0

    # 4. CQ46 の本文を読む
    body = tools.read_doc("/ext/doc/診療ガイドライン/肺癌2025_非小細胞癌/CQ47.md")
    assert "オシメルチニブ" in body
    assert "推奨の強さ: 1(強く推奨)" in body


# ---------------------------------------------------------------------------
# grep
# ---------------------------------------------------------------------------


def test_grep_returns_dict_with_matches(tools: Tools) -> None:
    result = tools.grep_doc("オシメルチニブ", path="/ext/doc/診療ガイドライン/肺癌2025_非小細胞癌/")
    assert isinstance(result, dict)
    assert result["query"] == "オシメルチニブ"
    assert result["regex"] is False
    assert result["match_count"] >= 1
    assert isinstance(result["matches"], list)
    for m in result["matches"]:
        assert isinstance(m["match_count"], dict)
        assert any(m["match_count"][f] for f in ("title", "description", "body"))


def test_grep_root_default(tools: Tools) -> None:
    """path 省略でも root subtree 検索になる (デフォルト `/ext/doc/`)。"""
    result = tools.grep_doc("EGFR")
    assert result["path"] == "/ext/doc/"
    assert result["match_count"] >= 1


def test_grep_regex_flag(tools: Tools) -> None:
    result = tools.grep_doc(
        r"Grade\s*[34]",
        path="/ext/doc/診療ガイドライン/CTCAEv5_JCOG_2025/血液およびリンパ系障害.md",
        regex=True,
    )
    assert result["regex"] is True
    assert result["match_count"] >= 1


def test_grep_bad_regex_raises_a_call_error(tools: Tools) -> None:
    """正規表現の構文エラーは ToolCallError (Python の英語の文面は出さない)。"""
    result = _error_text(lambda: tools.grep_doc("(unbalanced", regex=True), ToolCallError)
    assert result == "ERROR: query が正規表現として読めない。次の手: 書き直すか regex=false で探す。"


def test_grep_bad_path_raises_an_outcome(tools: Tools) -> None:
    result = _error_text(lambda: tools.grep_doc("test", path="/ext/doc/typo/"))
    assert "見つからない階層名: typo。" in result
    assert "診療ガイドライン" in result


def test_bad_path_without_leading_slash_raises_a_call_error(tools: Tools) -> None:
    assert _error_text(lambda: tools.read_doc("診療ガイドライン"), ToolCallError) == (
        "ERROR: doc の path が / 始まりの文字列でない (指定: 診療ガイドライン)。次の手: list_doc / grep_doc が返した `/ext/doc/…` の path で呼び直す。"
    )
