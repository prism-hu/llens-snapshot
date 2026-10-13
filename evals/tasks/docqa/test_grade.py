"""Tests for the grading rules of grade.py (no server needed).

Run: uv run --group evals --group dev pytest evals/tasks/docqa -q
"""

from __future__ import annotations

import pytest

from evals.tasks.docqa.grade import (
    bootstrap_ci,
    compare,
    extract_answer,
    extract_citation_line,
    grade_row,
    is_citation_correct,
    is_correct,
    load_questions,
    mcnemar_exact_p,
    normalize,
    render_compare,
    render_report,
    summarize,
)


# -- extract / normalize -------------------------------------------------------


def test_extract_answer_takes_last_answer_line():
    content = "考察...\n【回答】仮の答え\n再考...\n【回答】 **291点**\n【根拠】/x/y"
    text, found = extract_answer(content)
    assert found and text == "291点"


def test_extract_answer_falls_back_to_whole_content():
    text, found = extract_answer("初診料は291点です。")
    assert not found and text == "初診料は291点です。"


def test_extract_answer_accepts_colon_after_marker():
    text, found = extract_answer("【回答】: はい")
    assert found and text == "はい"


MULTILINE = """調べました。

【回答】
(1) 切迫性：患者本人の生命・身体が危険にさらされる可能性が著しく高い
(2) 非代替性：身体拘束以外に代替する方法がない
- (3) 一時性：身体拘束は一時的なものである

【根拠】
/院内マニュアル/医療安全管理マニュアル/身体拘束
"""


def test_extract_answer_multiline_block_until_citation():
    text, found = extract_answer(MULTILINE)
    assert found
    assert text.startswith("(1) 切迫性") and text.endswith("一時的なものである")
    assert "根拠" not in text and "院内マニュアル" not in text


def test_multiline_block_grades_list_correct():
    row = {"id": "x", "answer_kind": "list", "accept": ["切迫性", "非代替性", "一時性"],
           "gold_paths": ["/院内マニュアル/医療安全管理マニュアル/身体拘束"],
           "content": MULTILINE, "tool_calls": []}
    g = grade_row(row)
    assert g["answer_line_found"] and g["correct"] and g["citation_correct"]


def test_extract_answer_block_runs_to_end_without_citation():
    text, found = extract_answer("【回答】\n- はい\n- 算定可")
    assert found and text == "- はい\n- 算定可"


def test_extract_answer_tolerates_kon_kon_kyo_typo():
    text, found = extract_answer("【回答】\n291点\n【根根拠】/x/y")
    assert found and text == "291点"
    assert extract_citation_line("【回答】\n291点\n【根根拠】/x/y\nおまけ") == "/x/y"


def test_extract_answer_tolerates_halfwidth_brackets():
    text, found = extract_answer("[回答] 291点\n[根拠] /x/y")
    assert found and text == "291点"


def test_extract_answer_empty_last_block_falls_back_to_whole_content():
    content = "【回答】291点\n【回答】\n【根拠】/x"
    text, found = extract_answer(content)
    assert not found and text == content


def test_normalize_fullwidth_comma_space_case():
    assert normalize("２，９１０ 点 Triptan") == "2910点triptan"


# -- number --------------------------------------------------------------------


@pytest.mark.parametrize(
    "answer, accept, expected",
    [
        ("291点", ["291"], True),
        ("２９１点", ["291"], True),
        ("2,910点", ["2910"], True),
        ("291点", ["29"], False),  # 29 does not match 291
        ("129点", ["29"], False),
        ("29点", ["29"], True),
        ("約 29 点です", ["29"], True),
        ("291.5", ["291"], False),  # the integer part of a decimal alone does not match
        ("30日", ["30", "31"], True),  # any of them
        ("該当なし", ["30"], False),
        ("1.5050", ["1.505"], True),  # trailing zeros are ignored when numerically equal
        ("1.50", ["1.5"], True),
        ("0.5%", ["0.50"], True),
        ("1.505", ["1.5050"], True),
        ("約1,505.0 mg", ["1505"], True),
        ("291", ["291.5"], False),
        ("A000 は 291 点", ["1000"], False),  # the 000 in A000 is 0, not 1000
        ("-3℃", ["-3"], True),
        ("3", ["-3"], False),
        ("291点", ["x"], False),  # a non-numeric accept never matches
    ],
)
def test_number(answer, accept, expected):
    assert is_correct("number", accept, answer) is expected


# -- term / bool / list --------------------------------------------------------


def test_term_single_character_exact_match():
    # neuro_gl-003: when a bare "A" is added to accept. No length / boundary limit; a whole-block match is always correct
    assert is_correct("term", ["A"], "A") is True
    assert is_correct("term", ["グレード A", "A"], "Ａ") is True
    g = grade_row({"id": "x", "answer_kind": "term", "accept": ["A"], "gold_paths": ["/g"],
                   "content": "【回答】A\n\n【根拠】`/g`", "tool_calls": []})
    assert g["correct"] is True and g["citation_correct"] is True


def test_term_case_insensitive_and_variants():
    assert is_correct("term", ["トリプタン", "triptan"], "Triptan 系薬剤") is True
    assert is_correct("term", ["トリプタン", "triptan"], "ＮＳＡＩＤｓ") is False


def test_term_fullwidth_alnum_folds():
    assert is_correct("term", ["Grade 3"], "Ｇｒａｄｅ　３") is True


def test_bool():
    assert is_correct("bool", ["はい", "できる", "算定可"], "はい、算定できます") is True
    assert is_correct("bool", ["いいえ"], "はい") is False


def test_list_requires_all_items():
    accept = ["A", "B", "C"]
    assert is_correct("list", accept, "a, b, c の 3 つ") is True
    assert is_correct("list", accept, "a と b") is False
    assert is_correct("list", [], "anything") is False


# -- absent --------------------------------------------------------------------


@pytest.mark.parametrize(
    "answer, expected",
    [
        ("収録されていません", True),
        ("該当する区分は見当たりません", True),
        ("該当する文書はない", True),
        ("A000-9 という区分は存在しません", True),
        ("掲載がない", True),
        ("確認できませんでした", True),
        # forms that were correct refusals but failed in the 2026-09-08 live run
        ("未収録のため回答できません", True),
        ("該当文書は収録されておらず、点数は不明です", True),
        ("索引・全文とも確認できず", True),
        ("収録されていないため回答できません", True),
        ("この区分は掲載されておらず", True),
        ("該当する CQ が存在せず", True),
        ("含まれておらず", True),
        ("収録がありません", True),
        ("引くことができません", True),
        ("該当する記載を本コーパスから見当たらず", True),
        ("A000-9 は 300 点です", False),
        ("収録されています。300 点です", False),
    ],
)
def test_absent_phrases(answer, expected):
    assert is_correct("absent", [], answer) is expected


def test_absent_uses_answer_line_not_body_hedge():
    # saying 「確認できない」 in the body but fabricating a number in 【回答】 is wrong
    row = {
        "id": "x", "answer_kind": "absent", "accept": [], "gold_paths": [],
        "content": "索引では確認できませんでしたが…\n【回答】300点",
        "tool_calls": [],
    }
    g = grade_row(row)
    assert g["correct"] is False and g["citation_correct"] is False


def test_absent_citation_equals_correct():
    row = {
        "id": "x", "answer_kind": "absent", "accept": [], "gold_paths": [],
        "content": "【回答】収録されていません", "tool_calls": [],
    }
    g = grade_row(row)
    assert g["correct"] is True and g["citation_correct"] is True


# -- citation ------------------------------------------------------------------

GOLD = ["/診療報酬/医科点数表R08_基本診療料/A000_初診料"]


def test_citation_in_body():
    body = "【回答】291点\n【根拠】/診療報酬/医科点数表R08_基本診療料/A000_初診料"
    assert is_citation_correct(GOLD, body, []) is True


def test_citation_in_body_without_leading_slash_or_with_md():
    body = "根拠: 診療報酬/医科点数表R08_基本診療料/A000_初診料.md"
    assert is_citation_correct(GOLD, body, []) is True


def test_citation_via_read_doc_path_argument():
    calls = [
        {"name": "grep_doc", "arguments": {"pattern": "初診料"}},
        {"name": "read_doc", "arguments": {"path": "/診療報酬/医科点数表R08_基本診療料/A000_初診料"}},
    ]
    assert is_citation_correct(GOLD, "【回答】291点", calls) is True


def test_citation_via_read_doc_paths_list_argument():
    calls = [{"name": "read_doc", "arguments": {"paths": ["/foo", "診療報酬/医科点数表R08_基本診療料/A000_初診料.md"]}}]
    assert is_citation_correct(GOLD, "", calls) is True


def test_citation_not_from_other_tools_or_wrong_path():
    calls = [
        {"name": "list_doc", "arguments": {"path": "/診療報酬/医科点数表R08_基本診療料/A000_初診料"}},
        {"name": "read_doc", "arguments": {"path": "/診療報酬/医科点数表R08_基本診療料/A001_再診料"}},
    ]
    assert is_citation_correct(GOLD, "【回答】291点", calls) is False


def test_citation_empty_gold_is_false():
    assert is_citation_correct([], "anything", []) is False


# -- grade_row flags -----------------------------------------------------------


def test_grade_row_tool_and_sandbox_flags():
    row = {
        "id": "q", "domain": "dpc", "qtype": "filter", "answer_kind": "number", "accept": ["12"],
        "gold_paths": ["/診療報酬/DPC点数表R08/x"],
        "content": "【回答】12件", "n_tool_calls": 3,
        "tool_calls": [
            {"name": "list_doc", "arguments": {}},
            {"name": "run_python", "arguments": {"code": "..."}},
            {"name": "read_doc", "arguments": {"path": "/診療報酬/DPC点数表R08/x"}},
        ],
        "total_ms": 1234.5,
    }
    g = grade_row(row)
    assert g["correct"] and g["citation_correct"] and g["tool_used"] and g["used_sandbox"]
    assert g["n_tool_calls"] == 3


def _filter_row(i, content):
    return {"id": f"f{i}", "domain": "dpc", "qtype": "filter", "answer_kind": "number", "accept": ["12"],
            "gold_paths": [], "content": content, "tool_calls": [{"name": "run_python", "arguments": {}}]}


def test_filter_row_without_gold_has_na_citation():
    g = grade_row(_filter_row(0, "【回答】12件"))
    assert g["correct"] is True and g["citation_correct"] is None


def test_summarize_citation_denominator_excludes_na():
    graded = [
        grade_row(_filter_row(0, "【回答】12件")),
        grade_row(_filter_row(1, "【回答】3件")),
        grade_row({"id": "l0", "domain": "d", "qtype": "lookup", "answer_kind": "number", "accept": ["291"],
                   "gold_paths": ["/a/b"], "content": "【回答】291点\n【根拠】/a/b", "tool_calls": []}),
        grade_row({"id": "l1", "domain": "d", "qtype": "lookup", "answer_kind": "number", "accept": ["291"],
                   "gold_paths": ["/a/b"], "content": "【回答】291点\n【根拠】/zzz", "tool_calls": []}),
    ]
    s = summarize(graded)
    assert s["n"] == 4 and s["acc"] == 0.75
    assert s["n_cit"] == 2 and s["cit_acc"] == 0.5
    only_filter = summarize(graded[:2])
    assert only_filter["n_cit"] == 0 and only_filter["cit_acc"] is None
    report = render_report("t", graded)
    assert "n_cit" in report and "| overall | 4 | 75.0% | 2 | 50.0% |" in report


def test_grade_row_error_without_content_is_wrong():
    g = grade_row({"id": "q", "answer_kind": "absent", "accept": [], "gold_paths": [],
                   "content": "", "tool_calls": [], "error": "http 500"})
    assert g["correct"] is False and g["tool_used"] is False


# -- questions dir overrides result snapshot ------------------------------------


def test_grade_row_uses_current_questions_over_snapshot(tmp_path, capsys):
    # real case neuro_gl-003: "A" was added to accept after the run, but the snapshot on the results side lacks it
    (tmp_path / "neuro_gl.jsonl").write_text(
        '{"id": "neuro_gl-003", "domain": "neuro_gl", "qtype": "lookup", "answer_kind": "term", '
        '"accept": ["グレード A", "A"], "gold_paths": ["/g/new"]}\n', encoding="utf-8")
    (tmp_path / "dpc.jsonl").write_text('{"id": "dpc-001", "domain": "dpc", "qtype": "filter", '
                                        '"answer_kind": "number", "accept": ["12"], "gold_paths": []}\n',
                                        encoding="utf-8")
    qs = load_questions(tmp_path)
    assert set(qs) == {"neuro_gl-003", "dpc-001"}

    snapshot = {"id": "neuro_gl-003", "domain": "old", "qtype": "old", "answer_kind": "term",
                "accept": ["グレード A"], "gold_paths": ["/g/old"],
                "content": "【回答】A\n\n【根拠】/g/new", "tool_calls": []}
    assert grade_row(snapshot)["correct"] is False  # fails with the snapshot as-is
    g = grade_row(snapshot, qs)
    assert g["correct"] is True and g["citation_correct"] is True
    assert g["domain"] == "neuro_gl" and g["qtype"] == "lookup"
    assert capsys.readouterr().err == ""

    # an id not in questions is graded with the snapshot, with a warning on stderr
    orphan = {"id": "gone-999", "domain": "x", "qtype": "lookup", "answer_kind": "number", "accept": ["3"],
              "gold_paths": ["/p"], "content": "【回答】3\n【根拠】/p", "tool_calls": []}
    g = grade_row(orphan, qs)
    assert g["correct"] is True and g["domain"] == "x"
    assert "gone-999" in capsys.readouterr().err


# -- compare -------------------------------------------------------------------


def _g(i, correct):
    return {"id": f"q{i}", "domain": "d", "qtype": "lookup", "correct": correct,
            "citation_correct": correct, "tool_used": False}


def test_compare_paired_stats():
    a = [_g(i, i < 4) for i in range(10)]   # 4/10
    b = [_g(i, i < 8) for i in range(10)]   # 8/10
    r = compare(a, b, "correct")
    assert r["n"] == 10 and r["a"] == 0.4 and r["b"] == 0.8
    assert r["diff"] == pytest.approx(0.4)
    assert r["a_only"] == 0 and r["b_only"] == 4
    assert r["ci"][0] <= 0.4 <= r["ci"][1]
    assert r["p"] == pytest.approx(0.125)  # 2 * 0.5**4


def test_compare_citation_skips_na_rows_on_either_side():
    a = [_g(i, i < 4) for i in range(10)]
    b = [_g(i, i < 8) for i in range(10)]
    a[0]["citation_correct"] = None          # N/A on side A only
    b[9]["citation_correct"] = None          # N/A on side B only
    a[5]["citation_correct"] = b[5]["citation_correct"] = None
    r = compare(a, b, "citation_correct")
    assert r["n"] == 7
    assert r["a"] == pytest.approx(3 / 7) and r["b"] == pytest.approx(6 / 7)
    assert r["a_only"] == 0 and r["b_only"] == 3
    # the correct side is unaffected
    assert compare(a, b, "correct")["n"] == 10
    text = render_compare("A", "B", a, b)
    assert "| citation_correct | 7 |" in text and "| correct | 10 |" in text


def test_bootstrap_is_deterministic():
    diffs = [1, 0, -1, 1, 1, 0, 0, 1]
    assert bootstrap_ci(diffs) == bootstrap_ci(diffs)


def test_mcnemar_no_discordant_pairs():
    assert mcnemar_exact_p(0, 0) == 1.0
