"""Tests for the mon domain. No Prometheus needed (`mon._prom_get` is swapped out).

What is checked is the "named query contract": catalog shape (no PromQL exposed), now's
names / group resolution and 400 on unknown names, export's CSV shape and row limit,
duration parsing, 502 when Prometheus is unreachable, Grafana link derivation.
"""

from __future__ import annotations

import csv
import io

import pytest
from fastapi.testclient import TestClient

from cage import mon
from cage.server import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _instant(series: list[tuple[dict, str]]):
    return {"resultType": "vector",
            "result": [{"metric": m, "value": [1700000000, v]} for m, v in series]}


def _range(series: list[tuple[dict, list[tuple[int, str]]]]):
    return {"resultType": "matrix",
            "result": [{"metric": m, "values": [[ts, v] for ts, v in vals]} for m, vals in series]}


def fake_prom(monkeypatch, handler):
    """Swap out `_prom_get(path, params)` and record the calls."""
    calls: list[tuple[str, dict]] = []

    def _get(path, params):
        calls.append((path, params))
        return handler(path, params)

    monkeypatch.setattr(mon, "_prom_get", _get)
    return calls


# ---------------------------------------------------------------------------
# catalog
# ---------------------------------------------------------------------------


def test_catalog_shape(client, monkeypatch):
    monkeypatch.setattr(mon, "GRAFANA_BASE", "http://h:13400")
    r = client.post("/mon/catalog", json={})
    assert r.status_code == 200
    d = r.json()
    assert d["groups"] == ["gpu", "sglang", "sandbox"]
    names = [q["name"] for q in d["queries"]]
    assert "gpu_util" in names and "sglang_ttft_p95" in names and "sandbox_disk_used" in names
    assert len(names) == len(set(names))
    for q in d["queries"]:
        assert set(q) == {"name", "group", "title", "unit", "description"}
    assert d["grafana"] == {"sglang": "http://h:13400/d/sglang-h200",
                            "sandbox": "http://h:13400/d/llens-sandbox"}
    # PromQL appears nowhere in the response
    assert "DCGM_" not in r.text and "sglang:" not in r.text


def test_catalog_without_grafana(client, monkeypatch):
    monkeypatch.setattr(mon, "GRAFANA_BASE", "")
    assert client.post("/mon/catalog", json={}).json()["grafana"] == {}


def test_query_table_is_consistent():
    for q in mon.QUERIES:
        assert q["group"] in mon.DASHBOARD_UID
        assert isinstance(q["labels"], list)
        assert q["description"] and q["title"]


# ---------------------------------------------------------------------------
# now
# ---------------------------------------------------------------------------


def test_now_by_names_keeps_only_declared_labels_and_rounds(client, monkeypatch):
    monkeypatch.setattr(mon, "GRAFANA_BASE", "http://h:13400")

    def handler(path, params):
        assert path == "/api/v1/query"
        if "DCGM_FI_DEV_GPU_UTIL" in params["query"]:
            return _instant([({"gpu": "1", "UUID": "x", "instance": "i"}, "12.34567"),
                             ({"gpu": "0", "UUID": "y", "instance": "i"}, "3")])
        return _instant([({"instance": "i", "job": "sglang"}, "2")])

    calls = fake_prom(monkeypatch, handler)
    r = client.post("/mon/now", json={"names": ["gpu_util", "sglang_running"]})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["at"].endswith("+09:00") or "+" in d["at"] or "Z" in d["at"]
    assert [x["name"] for x in d["results"]] == ["gpu_util", "sglang_running"]
    gpu = d["results"][0]
    assert gpu["title"] == "GPU 使用率" and gpu["unit"] == "%"
    # labels other than gpu are dropped; sorted by gpu number
    assert gpu["series"] == [{"labels": {"gpu": "0"}, "value": 3.0},
                             {"labels": {"gpu": "1"}, "value": 12.346}]
    assert d["results"][1]["series"] == [{"labels": {}, "value": 2.0}]
    assert d["grafana"] == "http://h:13400/d/sglang-h200"
    # no PromQL came from the request = the table's expression is used as is
    assert calls[0][1]["query"] == mon.BY_NAME["gpu_util"]["promql"]


def test_now_by_group_returns_every_query_and_null_when_no_data(client, monkeypatch):
    monkeypatch.setattr(mon, "GRAFANA_BASE", "")
    fake_prom(monkeypatch, lambda path, params: _instant([]))
    r = client.post("/mon/now", json={"group": "sandbox"})
    assert r.status_code == 200
    d = r.json()
    expected = [q["name"] for q in mon.QUERIES if q["group"] == "sandbox"]
    assert [x["name"] for x in d["results"]] == expected
    for x in d["results"]:
        assert x["series"] == [{"labels": {}, "value": None}]
    assert d["grafana"] is None


def test_now_applies_scale(client, monkeypatch):
    fake_prom(monkeypatch, lambda p, params: _instant([({"gpu": "0"}, "2048")]))
    d = client.post("/mon/now", json={"names": ["gpu_vram_used"]}).json()
    assert d["results"][0]["unit"] == "GiB"
    assert d["results"][0]["series"][0]["value"] == 2.0


def test_now_nan_becomes_null(client, monkeypatch):
    fake_prom(monkeypatch, lambda p, params: _instant([({}, "NaN")]))
    d = client.post("/mon/now", json={"names": ["sglang_ttft_p95"]}).json()
    assert d["results"][0]["series"] == [{"labels": {}, "value": None}]


def test_now_unknown_name_400(client, monkeypatch):
    calls = fake_prom(monkeypatch, lambda p, params: _instant([]))
    r = client.post("/mon/now", json={"names": ["gpu_util", "nope"]})
    assert r.status_code == 400
    d = r.json()
    assert d["error"] == "unknown_name"
    assert "gpu_util" in d["known"]
    assert calls == []  # never reaches Prometheus


@pytest.mark.parametrize("body", [{}, {"names": ["gpu_util"], "group": "gpu"},
                                  {"group": "nope"}, {"names": []}, {"group": "all"}])
def test_now_bad_request_400(client, monkeypatch, body):
    fake_prom(monkeypatch, lambda p, params: _instant([]))
    r = client.post("/mon/now", json=body)
    assert r.status_code == 400
    assert r.json()["error"] == "bad_request"


def test_now_prometheus_unavailable_502(client, monkeypatch):
    def handler(path, params):
        raise mon.PrometheusUnavailable("connection refused")

    fake_prom(monkeypatch, handler)
    r = client.post("/mon/now", json={"group": "gpu"})
    assert r.status_code == 502
    assert r.json() == {"error": "prometheus_unavailable", "detail": "connection refused"}


def test_now_prometheus_error_502(client, monkeypatch):
    def handler(path, params):
        raise mon.PrometheusError("HTTP 422: bad query")

    fake_prom(monkeypatch, handler)
    r = client.post("/mon/now", json={"group": "gpu"})
    assert r.status_code == 502
    assert r.json()["error"] == "prometheus_error"


def test_real_prom_get_reports_unavailable(monkeypatch):
    """The real `_prom_get` raises PrometheusUnavailable for an unreachable base."""
    monkeypatch.setattr(mon, "PROM_BASE", "http://127.0.0.1:9")  # discard port; nobody listens
    monkeypatch.setattr(mon, "TIMEOUT_S", 1.0)
    with pytest.raises(mon.PrometheusUnavailable):
        mon._prom_get("/api/v1/query", {"query": "up"})


# ---------------------------------------------------------------------------
# duration
# ---------------------------------------------------------------------------


def test_parse_duration():
    assert mon.parse_duration("30m") == 1800
    assert mon.parse_duration("1h") == 3600
    assert mon.parse_duration("6h") == 21600
    assert mon.parse_duration("7d") == 7 * 86400
    assert mon.parse_duration("45s") == 45
    for bad in ("", "1", "h", "1.5h", "-1h", "1h30m", "0m", "now-1h"):
        with pytest.raises(ValueError):
            mon.parse_duration(bad)


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


def test_default_export_step():
    assert mon.default_export_step(600) == 5
    assert mon.default_export_step(3600) == 5
    assert mon.default_export_step(6 * 3600) == 11  # ceil(21600/2000)
    assert mon.default_export_step(7000) == 5       # 3.5 → 5s floor
    assert mon.default_export_step(30 * 86400) == 1296


def test_export_csv_shape_and_labels(client, monkeypatch):
    def handler(path, params):
        if path == "/api/v1/query":
            return _instant([({"gpu": "0"}, "1"), ({"gpu": "1"}, "1")])
        assert path == "/api/v1/query_range"
        assert params["step"] == 5
        start = params["start"]
        return _range([({"gpu": "1", "UUID": "x"}, [(start, "2048"), (start + 5, "NaN")]),
                       ({"gpu": "0", "UUID": "y"}, [(start, "1024")])])

    fake_prom(monkeypatch, handler)
    r = client.post("/mon/export", json={"names": ["gpu_vram_used"], "since": "10m"})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    assert r.headers["x-mon-rows"] == "3"
    assert r.headers["x-mon-step-s"] == "5"
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0] == ["ts", "time", "name", "labels", "value"]
    # ordered name → labels → ts; NaN is blank; scale (MiB → GiB) applied
    assert [(x[2], x[3], x[4]) for x in rows[1:]] == [
        ("gpu_vram_used", "gpu=0", "1.0"),
        ("gpu_vram_used", "gpu=1", "2.0"),
        ("gpu_vram_used", "gpu=1", ""),
    ]
    assert rows[1][0].isdigit()
    assert "T" in rows[1][1] and ("+" in rows[1][1] or "Z" in rows[1][1])


def test_export_scalar_series_has_empty_labels_and_group_all(client, monkeypatch):
    def handler(path, params):
        if path == "/api/v1/query":
            return _instant([({"instance": "i"}, "1")])
        return _range([({"instance": "i"}, [(params["start"], "7")])])

    calls = fake_prom(monkeypatch, handler)
    r = client.post("/mon/export", json={"group": "all", "since": "5m", "step": "1m"})
    assert r.status_code == 200
    rows = list(csv.reader(io.StringIO(r.text)))[1:]
    assert len(rows) == len(mon.QUERIES)
    assert rows[0][3] == ""  # no labels
    assert [x[2] for x in rows] == sorted(q["name"] for q in mon.QUERIES)
    assert r.headers["x-mon-step-s"] == "60"
    assert all(p["step"] == 60 for path, p in calls if path == "/api/v1/query_range")


def test_export_too_many_rows_before_fetch(client, monkeypatch):
    def handler(path, params):
        assert path == "/api/v1/query"  # never reaches query_range
        return _instant([({"gpu": str(i)}, "1") for i in range(8)])

    fake_prom(monkeypatch, handler)
    # 8 series × (7d / 5s + 1) ≈ 968k rows > 500k
    r = client.post("/mon/export", json={"names": ["gpu_util"], "since": "7d", "step": "5s"})
    assert r.status_code == 400
    d = r.json()
    assert d["error"] == "too_many_rows"
    assert d["estimated"] > mon.EXPORT_MAX_ROWS
    assert "step" in d["hint"]


def test_export_too_many_rows_after_fetch(client, monkeypatch):
    monkeypatch.setattr(mon, "EXPORT_MAX_ROWS", 10)

    def handler(path, params):
        if path == "/api/v1/query":
            return _instant([({}, "1")])  # estimate is 1 series × few rows → passes
        start = params["start"]
        return _range([({"gpu": str(g)}, [(start + i * 60, "1") for i in range(5)]) for g in range(4)])

    fake_prom(monkeypatch, handler)
    r = client.post("/mon/export", json={"names": ["gpu_util"], "since": "4m", "step": "1m"})
    assert r.status_code == 400 and r.json()["error"] == "too_many_rows"


def test_export_chunks_long_ranges(client, monkeypatch):
    """Ranges over 11,000 points/series are fetched in time chunks and concatenated."""
    def handler(path, params):
        if path == "/api/v1/query":
            return _instant([({}, "1")])
        start, end, step = params["start"], params["end"], params["step"]
        assert (end - start) // step + 1 <= mon.PROM_MAX_RESOLUTION
        return _range([({}, [(t, "1") for t in range(start, end + 1, step)])])

    calls = fake_prom(monkeypatch, handler)
    r = client.post("/mon/export", json={"names": ["sglang_queue"], "since": "1d", "step": "5s"})
    assert r.status_code == 200, r.text
    assert int(r.headers["x-mon-rows"]) == 86400 // 5 + 1
    assert sum(1 for p, _ in calls if p == "/api/v1/query_range") == 2


def test_export_errors(client, monkeypatch):
    fake_prom(monkeypatch, lambda p, params: _instant([]))
    r = client.post("/mon/export", json={"names": ["nope"]})
    assert r.status_code == 400 and r.json()["error"] == "unknown_name"
    r = client.post("/mon/export", json={})
    assert r.status_code == 400 and r.json()["error"] == "bad_request"
    r = client.post("/mon/export", json={"group": "gpu", "step": "fast"})
    assert r.status_code == 400 and r.json()["error"] == "bad_request"
    assert r.json()["detail"].startswith("step は")  # must not misreport it as since


def test_export_rejects_since_over_30d_instead_of_clamping(client, monkeypatch):
    """Over 30d is not silently clamped to 30d (the tool reports the requested since). 400 before fetching."""
    def handler(path, params):
        raise AssertionError("must not reach Prometheus")

    fake_prom(monkeypatch, handler)
    r = client.post("/mon/export", json={"group": "gpu", "since": "31d"})
    assert r.status_code == 400
    d = r.json()
    assert d["error"] == "bad_request" and "since は最大 30d" in d["detail"]
    # exactly 30d passes (boundary)
    fake_prom(monkeypatch, lambda p, params: _instant([]) if p == "/api/v1/query" else _range([]))
    assert client.post("/mon/export", json={"group": "gpu", "since": "30d"}).status_code == 200

    def down(path, params):
        raise mon.PrometheusUnavailable("down")

    fake_prom(monkeypatch, down)
    r = client.post("/mon/export", json={"group": "gpu"})
    assert r.status_code == 502 and r.json()["error"] == "prometheus_unavailable"


# ---------------------------------------------------------------------------
# grafana base
# ---------------------------------------------------------------------------


def test_derive_grafana_base():
    """Only the explicit CAGE_GRAFANA_BASE is used. Unset means no link (no derivation)."""
    assert mon.derive_grafana_base("http://g:3000/") == "http://g:3000"
    assert mon.derive_grafana_base(" http://g:3000 ") == "http://g:3000"
    assert mon.derive_grafana_base("") == ""
