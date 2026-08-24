import asyncio
import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any
from datetime import datetime, timedelta, timezone

import httpx

CONFIG_PATH = Path(os.getenv("PASSION_ADMIN_CONFIG", "/app/config/passion-admin.json"))
REPORT_RANGE = "15m"


def _unwrap(response: httpx.Response) -> Any:
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"Passion API returned invalid JSON (HTTP {response.status_code})") from exc
    if not response.is_success:
        message = payload.get("message", f"HTTP {response.status_code}") if isinstance(payload, dict) else f"HTTP {response.status_code}"
        raise RuntimeError(str(message)[:300])
    if isinstance(payload, dict) and "code" in payload:
        if payload.get("code") != 0:
            raise RuntimeError(str(payload.get("message", "Passion API request failed"))[:300])
        return payload.get("data")
    return payload


def load_admin_config() -> dict[str, str]:
    if not CONFIG_PATH.is_file():
        raise RuntimeError("Passion admin configuration is not mounted")
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("Passion admin configuration cannot be read") from exc
    base_url = str(config.get("base_url", "")).strip().rstrip("/")
    email = os.getenv("PASSION_ADMIN_EMAIL", "").strip() or str(config.get("admin_email", "")).strip()
    password = os.getenv("PASSION_ADMIN_PASSWORD", "") or str(config.get("admin_password", ""))
    if not base_url or not email or not password:
        raise RuntimeError("Passion admin URL, email, or password is missing")
    return {"base_url": base_url, "email": email, "password": password}


def _normalized_name(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _integer(value: Any, default: int = 0) -> int:
    number = _number(value)
    return max(0, int(number)) if number is not None else default


def _success_rate(metrics: dict[str, Any]) -> float | None:
    rate = _number(metrics.get("success_rate"))
    if rate is None:
        return None
    if 0 <= rate <= 1:
        rate *= 100
    return max(0.0, min(rate, 100.0))


def _duration_seconds(metrics: dict[str, Any]) -> float | None:
    value = metrics.get("latency_ms", metrics.get("avg_latency_ms"))
    if value is None and isinstance(metrics.get("ttft"), dict):
        value = metrics["ttft"].get("p50_ms")
    milliseconds = _number(value)
    return None if milliseconds is None else max(0.0, milliseconds / 1000)


def _first_token_seconds(metrics: dict[str, Any]) -> float | None:
    ttft = metrics.get("ttft") if isinstance(metrics.get("ttft"), dict) else {}
    value = ttft.get("avg_ms")
    if value is None:
        value = ttft.get("avg")
    if value is None:
        for key in ("avg_first_token_ms", "first_token_avg_ms", "avg_ms"):
            value = metrics.get(key)
            if value is not None:
                break
    milliseconds = _number(value)
    return None if milliseconds is None else max(0.0, milliseconds / 1000)


def _speed(metrics: dict[str, Any]) -> float | None:
    for key in ("tokens_per_second", "output_tokens_per_second", "avg_tokens_per_second"):
        value = _number(metrics.get(key))
        if value is not None:
            return max(0.0, value)
    duration = metrics.get("duration")
    average_ms = _number(duration.get("avg_ms")) if isinstance(duration, dict) else None
    output_tokens = _number(metrics.get("output_tokens"))
    success_requests = _number(metrics.get("success_requests"))
    if average_ms and average_ms > 0 and output_tokens is not None and success_requests and success_requests > 0:
        return max(0.0, output_tokens / success_requests / (average_ms / 1000))
    return None


def _model_card(listed: dict[str, Any], monitored: dict[str, Any] | None, usage: dict[str, int] | None = None) -> dict[str, Any]:
    name = str(listed.get("name", "")).strip()
    platform = str(listed.get("platform", "")).strip()
    if not monitored:
        total = int((usage or {}).get("total", 0))
        empty_count = int((usage or {}).get("empty_count", 0))
        return {"model_name": name, "platform": platform, "status": "inactive", "total": total, "success_rate": None, "empty_count": empty_count, "empty_rate": empty_count / total * 100 if total else None, "failure_count": 0, "average_duration": None, "average_first_token": None, "average_speed": None, "badges": []}

    metrics = monitored.get("metrics") if isinstance(monitored.get("metrics"), dict) else {}
    health = monitored.get("health") if isinstance(monitored.get("health"), dict) else {}
    total = int((usage or {}).get("total", _integer(metrics.get("request_count"))))
    rate = _success_rate(metrics) if total else None
    explicit_empty = (usage or {}).get("empty_count")
    success_count = _integer(metrics.get("success_requests"), round(total * (rate or 0) / 100))
    explicit_failure = metrics.get("failure_count", metrics.get("error_requests"))
    failure_count = _integer(explicit_failure, 0)
    if explicit_empty is None:
        empty_count = max(0, total - success_count - failure_count)
    else:
        empty_count = _integer(explicit_empty)
    if explicit_failure is None:
        failure_count = max(0, total - success_count - empty_count)
    duration = _duration_seconds(metrics)
    first_token = _first_token_seconds(metrics)
    speed = _speed(metrics)
    badges = []
    if rate is not None and rate >= 99:
        badges.append("recommended")
    if duration is not None and duration >= 60:
        badges.append("long")
    if speed is not None and speed < 10:
        badges.append("slow")
    status = str(health.get("overall", "unknown")).strip().lower()
    if status not in {"healthy", "warning", "critical"}:
        status = "unknown" if total else "inactive"
    return {"model_name": name, "platform": platform, "status": status, "total": total, "success_rate": rate, "empty_count": empty_count, "empty_rate": empty_count / total * 100 if total else None, "failure_count": failure_count, "average_duration": duration, "average_first_token": first_token, "average_speed": speed, "badges": badges}


def merge_group(group: dict[str, Any], monitored_items: list[dict[str, Any]], usage_stats: dict[str, dict[str, int]] | None = None) -> dict[str, Any]:
    listed = [item for item in group.get("models", []) if isinstance(item, dict) and str(item.get("name", "")).strip()]
    valid_monitored = [item for item in monitored_items if isinstance(item, dict)]
    name_counts = Counter(_normalized_name(item.get("model")) for item in valid_monitored)
    exact: dict[tuple[str, str], dict[str, Any]] = {}
    by_name: dict[str, dict[str, Any]] = {}
    for item in valid_monitored:
        name = _normalized_name(item.get("model"))
        platform = str(item.get("platform", "")).strip().casefold()
        if name:
            exact[(platform, name)] = item
            if name_counts[name] == 1:
                by_name[name] = item

    models = []
    for item in sorted(listed, key=lambda value: str(value.get("name", "")).casefold()):
        name = _normalized_name(item.get("name"))
        platform = str(item.get("platform", group.get("platform", ""))).strip()
        monitored = exact.get((platform.casefold(), name)) or by_name.get(name)
        models.append(_model_card({**item, "platform": platform}, monitored, (usage_stats or {}).get(name)))

    requests = sum(model["total"] for model in models)
    measured = [model for model in models if model["total"] and model["success_rate"] is not None]
    measured_requests = sum(model["total"] for model in measured)
    success_rate = sum(model["total"] * model["success_rate"] for model in measured) / measured_requests if measured_requests else None
    empty_count = sum(model["empty_count"] for model in models)
    return {"id": int(group["id"]), "name": str(group.get("name", "未命名分组")).strip() or "未命名分组", "totals": {"requests": requests, "success_rate": success_rate, "empty_count": empty_count, "empty_rate": empty_count / requests * 100 if requests else None}, "models": models}


def _usage_token(item: dict[str, Any]) -> float | None:
    value = item.get("output_tokens")
    if value is None and isinstance(item.get("usage"), dict):
        value = item["usage"].get("completion_tokens")
    return _number(value)


async def fetch_usage_stats(client: httpx.AsyncClient, base_url: str, auth: dict[str, str], group_id: int) -> dict[str, dict[str, int]]:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=15)
    page = 1
    start_time = cutoff.isoformat()
    end_time = now.isoformat()
    stats: dict[str, dict[str, int]] = {}
    while True:
        response = await client.get(f"{base_url}/api/v1/admin/usage", params={"page": page, "page_size": 1000, "group_id": group_id, "start_time": start_time, "end_time": end_time}, headers=auth)
        data = _unwrap(response)
        items = data.get("items", []) if isinstance(data, dict) else []
        reached_cutoff = False
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                created = datetime.fromisoformat(str(item.get("created_at", "")).replace("Z", "+00:00"))
            except ValueError:
                continue
            if created < cutoff:
                reached_cutoff = True
                continue
            name = _normalized_name(item.get("model"))
            if not name:
                continue
            bucket = stats.setdefault(name, {"total": 0, "empty_count": 0})
            bucket["total"] += 1
            output_tokens = _usage_token(item)
            if output_tokens is not None and output_tokens < 10:
                bucket["empty_count"] += 1
        pages = _integer(data.get("pages"), 1) if isinstance(data, dict) else 1
        if reached_cutoff or page >= pages or not items:
            break
        page += 1
    return stats


async def fetch_group_report(group_id: int | None = None) -> dict[str, Any]:
    report_to = datetime.now(timezone.utc)
    report_from = report_to - timedelta(minutes=15)
    config = load_admin_config()
    headers = {"Accept": "application/json", "Accept-Language": "zh", "x-admin-ui-request": "1"}
    async with httpx.AsyncClient(timeout=30, headers=headers) as client:
        login_data = _unwrap(await client.post(f"{config['base_url']}/api/v1/auth/login", json={"email": config["email"], "password": config["password"]}))
        token = login_data.get("access_token") if isinstance(login_data, dict) else None
        if not token:
            raise RuntimeError("Passion login response does not include an access token")
        auth = {"Authorization": f"Bearer {token}"}
        # Some Passion deployments disable the model plaza. Channel monitor
        # dimensions still provide the authoritative group list, so treat the
        # plaza as optional instead of failing the entire report.
        try:
            plaza = _unwrap(await client.get(f"{config['base_url']}/api/v1/model-plaza", headers=auth))
            raw_groups = plaza.get("groups", []) if isinstance(plaza, dict) else []
        except (httpx.HTTPError, RuntimeError, ValueError, TypeError):
            raw_groups = []
        groups = [item for item in raw_groups if isinstance(item, dict) and str(item.get("id", "")).isdigit()]
        groups.sort(key=lambda item: str(item.get("name", "")).casefold())
        try:
            dimensions = _unwrap(await client.get(f"{config['base_url']}/api/v1/admin/channel-monitor-v2/dimensions", params={"range": REPORT_RANGE}, headers=auth))
            dimension_groups = dimensions.get("groups", []) if isinstance(dimensions, dict) else []
            dimension_by_id = {int(item["id"]): item for item in dimension_groups if isinstance(item, dict) and str(item.get("id", "")).isdigit()}
            if dimension_by_id:
                listed_by_id = {int(item["id"]): item for item in groups}
                for group_id_value, dimension in dimension_by_id.items():
                    if group_id_value not in listed_by_id:
                        listed_by_id[group_id_value] = {"id": group_id_value, "name": dimension.get("name", "未命名分组"), "platform": dimension.get("platform", ""), "models": []}
                    else:
                        listed_by_id[group_id_value]["name"] = dimension.get("name", listed_by_id[group_id_value].get("name", "未命名分组"))
                groups = [listed_by_id[group_id_value] for group_id_value in dimension_by_id]
                groups.sort(key=lambda item: str(item.get("name", "")).casefold())
        except (httpx.HTTPError, RuntimeError, ValueError, TypeError):
            # Older deployments may not expose dimensions; retain plaza behavior.
            pass
        if group_id is not None:
            groups = [item for item in groups if int(item["id"]) == group_id]
            if not groups:
                raise LookupError("没有找到指定分组")

        async def monitor(group: dict[str, Any]) -> list[dict[str, Any]]:
            response = await client.get(f"{config['base_url']}/api/v1/admin/channel-monitor-v2/models", params={"range": REPORT_RANGE, "group_id": int(group["id"])}, headers=auth)
            data = _unwrap(response)
            items = data.get("items", []) if isinstance(data, dict) else []
            return [item for item in items if isinstance(item, dict)]

        monitored_groups = await asyncio.gather(*(monitor(group) for group in groups))
        usage_groups = await asyncio.gather(
            *(fetch_usage_stats(client, config["base_url"], auth, int(group["id"])) for group in groups)
        )

    merged = [merge_group(group, monitored, usage) for group, monitored, usage in zip(groups, monitored_groups, usage_groups)]
    requests = sum(group["totals"]["requests"] for group in merged)
    measured = [group for group in merged if group["totals"]["requests"] and group["totals"]["success_rate"] is not None]
    measured_requests = sum(group["totals"]["requests"] for group in measured)
    success_rate = sum(group["totals"]["requests"] * group["totals"]["success_rate"] for group in measured) / measured_requests if measured_requests else None
    empty_count = sum(group["totals"]["empty_count"] for group in merged)
    return {"range": REPORT_RANGE, "range_label": "最近 15 分钟", "from": report_from.isoformat(), "to": report_to.isoformat(), "totals": {"requests": requests, "success_rate": success_rate, "empty_count": empty_count, "empty_rate": empty_count / requests * 100 if requests else None}, "groups": merged}
