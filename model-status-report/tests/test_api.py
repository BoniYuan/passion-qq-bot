from fastapi.testclient import TestClient
from app import main

SAMPLE = {"range": "15m", "range_label": "最近 15 分钟", "totals": {"requests": 0, "success_rate": None}, "groups": []}


def test_report_and_export(monkeypatch):
    calls = []
    async def fake(group_id=None):
        calls.append(group_id)
        return SAMPLE
    monkeypatch.setattr(main, "fetch_group_report", fake)
    with TestClient(main.app) as client:
        assert client.get("/api/reports/summary").json() == SAMPLE
        assert client.get("/api/reports/summary?group_id=7").json() == SAMPLE
        assert client.get("/api/reports/export?group_id=7").headers["content-type"].startswith("application/json")
    assert calls == [None, 7, 7]


def test_report_errors_and_removed_routes(monkeypatch):
    async def missing(_=None):
        raise LookupError("没有找到指定分组")
    monkeypatch.setattr(main, "fetch_group_report", missing)
    with TestClient(main.app) as client:
        response = client.get("/api/reports/summary?group_id=9")
        assert response.status_code == 404
        assert response.json()["code"] == "group_not_found"
        assert client.get("/api/channels").status_code == 404
        assert client.get("/api/settings").status_code == 404
        assert client.post("/api/probes/run", json={}).status_code == 404


def test_unknown_api_does_not_fall_back_to_spa(monkeypatch, tmp_path):
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("app", encoding="utf-8")
    monkeypatch.setattr(main, "STATIC_DIR", static_dir)
    with TestClient(main.app) as client:
        response = client.get("/api/channels")
        assert response.status_code == 404
        assert response.headers["content-type"].startswith("application/json")


def test_upstream_failure(monkeypatch):
    async def failed(_=None):
        raise RuntimeError("upstream unavailable")
    monkeypatch.setattr(main, "fetch_group_report", failed)
    with TestClient(main.app) as client:
        response = client.get("/api/reports/summary")
        assert response.status_code == 502
        assert response.json()["code"] == "passion_api_failed"
