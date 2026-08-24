import json
import httpx
import pytest
from datetime import datetime
from app import catalog


def test_merge_group_matches_platform_and_keeps_unmonitored_models():
    group = {"id": 2, "name": "B 组", "models": [{"name": "same", "platform": "a"}, {"name": "same", "platform": "b"}, {"name": "idle", "platform": "a"}]}
    monitored = [
        {"model": "same", "platform": "a", "metrics": {"request_count": 10, "success_rate": 0.9, "empty_count": 1, "latency_ms": 1500}, "health": {"overall": "warning"}},
        {"model": "same", "platform": "b", "metrics": {"request_count": 20, "success_rate": 95, "failure_count": 1, "tokens_per_second": 8}, "health": {"overall": "healthy"}},
    ]
    result = catalog.merge_group(group, monitored)
    assert [item["model_name"] for item in result["models"]] == ["idle", "same", "same"]
    assert result["models"][0]["status"] == "inactive"
    assert result["models"][0]["success_rate"] is None
    assert result["models"][1]["total"] == 10
    assert result["models"][2]["total"] == 20
    assert result["totals"]["success_rate"] == pytest.approx((10 * 90 + 20 * 95) / 30)


def test_unique_name_fallback_and_ambiguous_name_not_matched():
    group = {"id": 1, "name": "A", "models": [{"name": "unique", "platform": "listed"}, {"name": "duplicate", "platform": "listed"}]}
    monitored = [{"model": "unique", "platform": "other", "metrics": {"request_count": 3, "success_rate": 1}}, {"model": "duplicate", "platform": "one", "metrics": {"request_count": 4}}, {"model": "duplicate", "platform": "two", "metrics": {"request_count": 5}}]
    models = catalog.merge_group(group, monitored)["models"]
    assert models[1]["total"] == 3
    assert models[0]["total"] == 0


@pytest.mark.asyncio
async def test_fetch_report_sorts_groups_filters_and_aggregates(monkeypatch, tmp_path):
    config_path = tmp_path / "admin.json"
    config_path.write_text(json.dumps({"base_url": "https://passion.test", "admin_email": "a@example.com", "admin_password": "secret"}), encoding="utf-8")
    monkeypatch.setattr(catalog, "CONFIG_PATH", config_path)

    def handler(request: httpx.Request):
        if request.url.path.endswith("/auth/login"):
            return httpx.Response(200, json={"code": 0, "data": {"access_token": "token"}})
        assert request.headers["Authorization"] == "Bearer token"
        if request.url.path.endswith("/channel-monitor-v2/dimensions") or request.url.path.endswith("/channel-monitor-v2/models"):
            assert request.url.params["range"] == "15m"
        if request.url.path.endswith("/model-plaza"):
            return httpx.Response(200, json={"code": 0, "data": {"groups": [{"id": 2, "name": "Zulu", "models": [{"name": "z", "platform": "p"}]}, {"id": 1, "name": "Alpha", "models": [{"name": "a", "platform": "p"}]}, {"id": None, "name": "invalid"}]}})
        if request.url.path.endswith("/channel-monitor-v2/dimensions"):
            return httpx.Response(200, json={"code": 0, "data": {"items": []}})
        group_id = request.url.params["group_id"]
        count, name = (2, "a") if group_id == "1" else (3, "z")
        return httpx.Response(200, json={"code": 0, "data": {"items": [{"model": name, "platform": "p", "metrics": {"request_count": count, "success_rate": 1}}]}})

    original = httpx.AsyncClient
    monkeypatch.setattr(catalog.httpx, "AsyncClient", lambda *args, **kwargs: original(transport=httpx.MockTransport(handler), headers=kwargs.get("headers"), timeout=kwargs.get("timeout")))
    report = await catalog.fetch_group_report()
    assert [group["name"] for group in report["groups"]] == ["Alpha", "Zulu"]
    assert report["from"] < report["to"]
    assert report["range"] == "15m"
    assert report["range_label"] == "最近 15 分钟"
    assert 899 <= (datetime.fromisoformat(report["to"]) - datetime.fromisoformat(report["from"])).total_seconds() <= 901
    assert report["totals"] == {"requests": 5, "success_rate": 100.0, "empty_count": 0, "empty_rate": 0.0}
    assert [group["id"] for group in (await catalog.fetch_group_report(2))["groups"]] == [2]
    with pytest.raises(LookupError):
        await catalog.fetch_group_report(999)


@pytest.mark.asyncio
async def test_fetch_report_uses_channel_monitor_dimensions(monkeypatch, tmp_path):
    config_path = tmp_path / "admin.json"
    config_path.write_text(json.dumps({"base_url": "https://passion.test", "admin_email": "a@example.com", "admin_password": "secret"}), encoding="utf-8")
    monkeypatch.setattr(catalog, "CONFIG_PATH", config_path)

    def handler(request: httpx.Request):
        if request.url.path.endswith("/auth/login"):
            return httpx.Response(200, json={"code": 0, "data": {"access_token": "token"}})
        if request.url.path.endswith("/model-plaza"):
            return httpx.Response(200, json={"code": 0, "data": {"groups": [
                {"id": 1, "name": "Visible", "models": [{"name": "m", "platform": "p"}]},
                {"id": 2, "name": "Sales only", "models": [{"name": "s", "platform": "p"}]},
            ]}})
        if request.url.path.endswith("/channel-monitor-v2/dimensions"):
            return httpx.Response(200, json={"code": 0, "data": {"groups": [
                {"id": 1, "name": "Visible", "platform": "p", "request_count": 1},
                {"id": 2, "name": "No calls", "platform": "p", "request_count": 0},
            ]}})
        if request.url.path.endswith("/channel-monitor-v2/models"):
            items = [{"model": "m", "platform": "p", "metrics": {"request_count": 1, "success_rate": 1}}] if request.url.params["group_id"] == "1" else []
            return httpx.Response(200, json={"code": 0, "data": {"items": items}})
        return httpx.Response(200, json={"code": 0, "data": {"items": [], "pages": 1}})

    original = httpx.AsyncClient
    monkeypatch.setattr(catalog.httpx, "AsyncClient", lambda *args, **kwargs: original(transport=httpx.MockTransport(handler), headers=kwargs.get("headers"), timeout=kwargs.get("timeout")))
    report = await catalog.fetch_group_report()
    assert [group["name"] for group in report["groups"]] == ["No calls", "Visible"]
    assert (await catalog.fetch_group_report(2))["groups"][0]["name"] == "No calls"


def test_merge_group_tolerates_invalid_metrics():
    group = {"id": 1, "name": "A", "models": [{"name": "m"}]}
    item = {"model": "m", "metrics": {"request_count": "bad", "success_rate": "bad", "latency_ms": {}}, "health": []}
    model = catalog.merge_group(group, [item])["models"][0]
    assert model["total"] == 0
    assert model["success_rate"] is None


def test_speed_is_derived_from_output_tokens_and_average_duration():
    group = {"id": 1, "name": "A", "models": [{"name": "m", "platform": "p"}]}
    monitored = [{
        "model": "m",
        "platform": "p",
        "metrics": {
            "request_count": 10,
            "success_requests": 8,
            "success_rate": 0.8,
            "output_tokens": 1600,
            "duration": {"avg_ms": 20000},
        },
    }]
    model = catalog.merge_group(group, monitored)["models"][0]
    assert model["average_speed"] == 10


def test_first_token_average_supports_ttft_and_millisecond_aliases():
    group = {"id": 1, "name": "A", "models": [{"name": "m", "platform": "p"}, {"name": "n", "platform": "p"}]}
    monitored = [
        {"model": "m", "platform": "p", "metrics": {"request_count": 1, "ttft": {"avg_ms": "7200"}}},
        {"model": "n", "platform": "p", "metrics": {"request_count": 1, "first_token_avg_ms": 10000}},
    ]
    models = catalog.merge_group(group, monitored)["models"]
    assert models[0]["average_first_token"] == 10
    assert models[1]["average_first_token"] == 7.2


def test_first_token_average_missing_is_null():
    group = {"id": 1, "name": "A", "models": [{"name": "m", "platform": "p"}]}
    model = catalog.merge_group(group, [{"model": "m", "platform": "p", "metrics": {"request_count": 1}}])["models"][0]
    assert model["average_first_token"] is None


def test_empty_count_is_residual_when_upstream_only_has_success_and_errors():
    group = {"id": 1, "name": "A", "models": [{"name": "m", "platform": "p"}]}
    monitored = [{"model": "m", "platform": "p", "metrics": {"request_count": 10, "success_requests": 6, "error_requests": 2, "success_rate": 0.6}}]
    model = catalog.merge_group(group, monitored)["models"][0]
    assert model["empty_count"] == 2
    assert model["failure_count"] == 2


def test_usage_token_threshold_and_nested_completion_tokens():
    group = {"id": 1, "name": "A", "models": [{"name": "m", "platform": "p"}]}
    usage = {"m": {"total": 4, "empty_count": 2}}
    model = catalog.merge_group(group, [{"model": "m", "platform": "p", "metrics": {"request_count": 4, "success_requests": 4, "success_rate": 1}}], usage)["models"][0]
    assert model["empty_rate"] == 50
    assert catalog._usage_token({"output_tokens": "9.99"}) == 9.99
    assert catalog._usage_token({"usage": {"completion_tokens": 10}}) == 10
    assert catalog._usage_token({"output_tokens": None}) is None
