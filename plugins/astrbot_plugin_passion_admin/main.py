import asyncio
import json
import os
import re
import secrets
import sqlite3
import time
import uuid
import hashlib
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import At, Plain, Reply
from astrbot.core.message.message_event_result import MessageChain


EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
EMAIL_IN_TEXT_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
REDEEM_REQUEST_PATTERNS = (
    re.compile(
        r"^兑换码\s+(?P<amount>\d+(?:\.\d{1,2})?)(?:\s+(?P<count>\d+))?$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:请)?(?:帮我)?生成(?:(?:一个)|(?P<count>\d+)张)?\s*"
        r"(?P<amount>\d+(?:\.\d{1,2})?)\s*(?:刀|美元|usd)?(?:的)?兑换码$",
        re.IGNORECASE,
    ),
)
MONITORED_GROUP_RULES = (
    ("余额", "gemini反重力", "¥0.24/刀"),
    ("按次", "gemini反重力"),
    ("酒馆按次", "¥0.4-0.5/次", "逆向cc"),
    ("酒馆按量", "¥0.35-0.44/刀", "逆向cc"),
    ("酒馆按量", "¥0.48-0.6/刀", "逆向cc"),
    ("酒馆按量", "¥1.20-1.50/刀", "ccmax"),
    ("酒馆按量", "¥2.6-3.2/刀", "awsbcc"),
)
ADMIN_COMMAND_RE = re.compile(
    r"[/／]\s*(?:监控分组|模型状态|模型监控|查询额度|机器人功能|"
    r"确认操作|充值帮助|兑换码|充值|退款|用户|余额)(?:\s|$)"
)
@register("astrbot_plugin_passion_admin", "local", "Passion 群聊管理助手", "0.9.1")
class PassionAdminPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.pending: dict[str, dict[str, Any]] = {}
        self.auto_credit_locks: dict[int, asyncio.Lock] = {}
        self.monitor_dimensions_cache: tuple[float, dict[str, Any]] | None = None
        self.monitor_models_cache: dict[int, tuple[float, list[dict[str, Any]]]] = {}
        self.model_plaza_cache: tuple[float, list[dict[str, Any]]] | None = None
        self.group_reminder_tasks: dict[str, asyncio.Task] = {}
        self.group_reminder_configs: dict[str, dict[str, Any]] = {}
        self.admin_token: str | None = None
        self.data_dir = Path("data/plugin_data/astrbot_plugin_passion_admin")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.group_reminder_path = self.data_dir / "group_reminders.json"
        self._load_group_reminder_configs()
        self.group_welcome_path = self.data_dir / "group_welcome_settings.json"
        self.group_welcome_settings = self._load_group_welcome_settings()
        self.db_path = self.data_dir / "operations.db"
        self.instance_token = uuid.uuid4().hex
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS operations (
                    operation_id TEXT PRIMARY KEY,
                    qq_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    email TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    notes TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

            db.execute(
                """
                CREATE TABLE IF NOT EXISTS request_claims (
                    fingerprint TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            claim_columns = {
                row[1] for row in db.execute("PRAGMA table_info(request_claims)").fetchall()
            }
            if "claimed_at" not in claim_columns:
                db.execute(
                    "ALTER TABLE request_claims ADD COLUMN claimed_at REAL NOT NULL DEFAULT 0"
                )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS plugin_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                INSERT INTO plugin_state(key, value) VALUES ('current_instance', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (self.instance_token,),
            )

    def _load_group_reminder_configs(self) -> None:
        try:
            data = json.loads(self.group_reminder_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self.group_reminder_configs = {str(k): v for k, v in data.items() if isinstance(v, dict) and str(v.get("origin", "")).strip() and int(v.get("minutes", 0)) >= 1}
        except (OSError, ValueError, TypeError):
            self.group_reminder_configs = {}

    def _save_group_reminder_configs(self) -> None:
        tmp_path = self.group_reminder_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(self.group_reminder_configs, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(self.group_reminder_path)

    def _load_group_welcome_settings(self) -> dict[str, bool]:
        try:
            data = json.loads(self.group_welcome_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(group_id): enabled for group_id, enabled in data.items() if isinstance(enabled, bool)}

    def _save_group_welcome_settings(self) -> None:
        tmp_path = self.group_welcome_path.with_suffix(".tmp")
        tmp_path.write_text(
            json.dumps(self.group_welcome_settings, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(self.group_welcome_path)

    def _welcome_enabled(self, group_id: str) -> bool:
        # Preserve the plugin's existing behavior until an admin disables a group.
        return self.group_welcome_settings.get(group_id, True)

    async def initialize(self) -> None:
        for group_id, item in self.group_reminder_configs.items():
            self.group_reminder_tasks[group_id] = asyncio.create_task(self._group_reminder_loop(group_id, str(item["origin"]), int(item["minutes"])))

    async def terminate(self) -> None:
        for task in self.group_reminder_tasks.values():
            task.cancel()
        if self.group_reminder_tasks:
            await asyncio.gather(*self.group_reminder_tasks.values(), return_exceptions=True)
        self.group_reminder_tasks.clear()

    @staticmethod
    def _is_private(event: AstrMessageEvent) -> bool:
        return not bool(event.get_group_id())

    @staticmethod
    def _is_mentioned_in_group(event: AstrMessageEvent) -> bool:
        if not event.get_group_id():
            return True
        message_obj = getattr(event, "message_obj", None)
        raw = getattr(message_obj, "raw_message", None)
        self_id = getattr(message_obj, "self_id", None)
        if not self_id:
            getter = getattr(event, "get_self_id", None)
            if callable(getter):
                self_id = getter()
        if isinstance(raw, list):
            mentions = [
                segment for segment in raw
                if isinstance(segment, dict)
                and str(segment.get("type", "")).lower() in {"at", "mention"}
            ]
            if not mentions:
                return False
            if not self_id:
                return True
            return any(
                str((segment.get("data") or {}).get("qq", "")) == str(self_id)
                for segment in mentions
            )
        return False

    def _admin_ids(self) -> set[str]:
        raw = str(self.config.get("admin_qq_ids", "610706314"))
        return {item.strip() for item in raw.split(",") if item.strip()}

    def _test_credit_admin_ids(self) -> set[str]:
        raw = str(self.config.get("test_credit_admin_qq_ids", ""))
        return {item.strip() for item in raw.split(",") if item.strip()}

    def _authorized(self, event: AstrMessageEvent) -> bool:
        return str(event.get_sender_id()) in self._admin_ids()

    def _test_credit_authorized(self, event: AstrMessageEvent) -> bool:
        sender_id = str(event.get_sender_id())
        return sender_id in self._admin_ids() or sender_id in self._test_credit_admin_ids()

    @staticmethod
    def _valid_account_email(email: str) -> bool:
        if not EMAIL_RE.fullmatch(email):
            return False
        domain = email.rsplit("@", 1)[1].lower()
        # Treat common QQ-domain typos such as qq.co as a format error.
        return not domain.startswith("qq.") or domain == "qq.com"

    @staticmethod
    def _is_slash_command_event(event: AstrMessageEvent) -> bool:
        """Inspect both normalized and OneBot raw text before auto-crediting."""
        # AstrBot removes a configured wake prefix (normally "/") from
        # message_str before plugin handlers run, but preserves this flag.
        if bool(getattr(event, "is_at_or_wake_command", False)):
            return True
        candidates: list[str] = []
        normalized = str(getattr(event, "message_str", "") or "").strip()
        if normalized:
            candidates.append(normalized)
        getter = getattr(event, "get_message_str", None)
        if callable(getter):
            value = str(getter() or "").strip()
            if value:
                candidates.append(value)

        def collect_text(value: Any) -> None:
            if isinstance(value, dict):
                segment_type = str(value.get("type", "")).lower()
                data = value.get("data")
                if segment_type == "text" and isinstance(data, dict):
                    text = str(data.get("text", "") or "").strip()
                    if text:
                        candidates.append(text)
                for key in ("message", "raw_message"):
                    if key in value:
                        collect_text(value[key])
            elif isinstance(value, list):
                for item in value:
                    collect_text(item)
            elif isinstance(value, str) and value.strip():
                candidates.append(value.strip())

        message_obj = getattr(event, "message_obj", None)
        for component in getattr(message_obj, "message", []) or []:
            component_text = str(getattr(component, "text", "") or "").strip()
            if component_text:
                candidates.append(component_text)
        collect_text(getattr(message_obj, "raw_message", None))
        return any(
            text.startswith(("/", "／"))
            or bool(ADMIN_COMMAND_RE.search(text))
            for text in candidates
        )

    def _private_authorized(self, event: AstrMessageEvent) -> bool:
        return self._is_private(event) and self._authorized(event)

    @staticmethod
    def _conversation_key(event: AstrMessageEvent) -> str:
        group_id = str(event.get_group_id() or "").strip()
        if group_id:
            return f"group:{group_id}"
        return f"private:{event.get_sender_id()}"

    def _is_current_instance(self) -> bool:
        with sqlite3.connect(self.db_path) as db:
            row = db.execute(
                "SELECT value FROM plugin_state WHERE key = 'current_instance'"
            ).fetchone()
        return bool(row and row[0] == self.instance_token)

    def _base_url(self) -> str:
        value = str(self.config.get("base_url", "https://api.passionapi.com")).strip()
        if not value:
            raise ValueError("管理员尚未配置 Passion API 地址。")
        return value.rstrip("/") + "/"

    async def _http(
        self,
        method: str,
        endpoint: str,
        *,
        token: str | None = None,
        body: dict[str, Any] | None = None,
    ) -> tuple[int, Any]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh",
            "User-Agent": "AstrBot-PassionAdmin/0.1",
            "x-admin-ui-request": "1",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        timeout = aiohttp.ClientTimeout(total=int(self.config.get("request_timeout", 15)))
        url = urljoin(self._base_url(), endpoint.lstrip("/"))
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(method, url, headers=headers, json=body) as response:
                text = await response.text()
                try:
                    payload = await response.json(content_type=None)
                except (ValueError, aiohttp.ContentTypeError):
                    payload = {"message": text[:300]}
                return response.status, payload

    @staticmethod
    def _unwrap(status: int, payload: Any) -> Any:
        if not 200 <= status < 300:
            message = payload.get("message", f"HTTP {status}") if isinstance(payload, dict) else f"HTTP {status}"
            raise ValueError(str(message)[:300])
        if isinstance(payload, dict) and "code" in payload:
            if payload.get("code") != 0:
                raise ValueError(str(payload.get("message", "接口返回失败"))[:300])
            return payload.get("data")
        return payload

    async def _login(self) -> str:
        email = os.environ.get("PASSION_ADMIN_EMAIL", "").strip() or str(
            self.config.get("admin_email", "")
        ).strip()
        password = os.environ.get("PASSION_ADMIN_PASSWORD", "") or str(
            self.config.get("admin_password", "")
        )
        if not email or not password:
            raise ValueError("请先在插件配置页填写管理员邮箱和密码。")
        status, payload = await self._http(
            "POST", "/api/v1/auth/login", body={"email": email, "password": password}
        )
        data = self._unwrap(status, payload)
        if not isinstance(data, dict) or not data.get("access_token"):
            raise ValueError("登录响应中没有访问令牌。")
        self.admin_token = str(data["access_token"])
        return self.admin_token

    async def _admin_api(
        self, method: str, endpoint: str, body: dict[str, Any] | None = None
    ) -> Any:
        token = self.admin_token or await self._login()
        status, payload = await self._http(method, endpoint, token=token, body=body)
        if status == 401:
            token = await self._login()
            status, payload = await self._http(method, endpoint, token=token, body=body)
        return self._unwrap(status, payload)

    async def _monitor_dimensions(self, time_range: str = "15m") -> dict[str, Any]:
        now = time.monotonic()
        if (
            self.monitor_dimensions_cache
            and now - self.monitor_dimensions_cache[0] < 30
        ):
            return self.monitor_dimensions_cache[1]
        data = await self._admin_api(
            "GET", f"/api/v1/admin/channel-monitor-v2/dimensions?range={time_range}"
        )
        dimensions = data if isinstance(data, dict) else {}
        self.monitor_dimensions_cache = (now, dimensions)
        return dimensions

    async def _monitor_models(self, group_id: int, time_range: str = "15m") -> list[dict[str, Any]]:
        now = time.monotonic()
        cached = self.monitor_models_cache.get(group_id)
        if time_range == "15m" and cached and now - cached[0] < 30:
            return cached[1]
        query = urlencode({"range": time_range, "group_id": group_id})
        data = await self._admin_api(
            "GET", f"/api/v1/admin/channel-monitor-v2/models?{query}"
        )
        items = data.get("items", []) if isinstance(data, dict) else []
        models = [item for item in items if isinstance(item, dict)]
        if time_range == "15m":
            self.monitor_models_cache[group_id] = (now, models)
        return models

    async def _model_plaza_groups(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        if self.model_plaza_cache and now - self.model_plaza_cache[0] < 60:
            return self.model_plaza_cache[1]
        data = await self._admin_api("GET", "/api/v1/model-plaza")
        items = data.get("groups", []) if isinstance(data, dict) else []
        groups = [
            item
            for item in items
            if isinstance(item, dict)
            and item.get("id") is not None
            and str(item.get("name", "")).strip()
        ]
        # Match the Passion channel-monitor selector, including groups with
        # zero requests; the model plaza also contains sales-only groups.
        try:
            dimensions = await self._monitor_dimensions("15m")
            dimension_groups = dimensions.get("groups", []) if isinstance(dimensions, dict) else []
            dimension_by_id = {
                int(item["id"]): item
                for item in dimension_groups
                if isinstance(item, dict) and str(item.get("id", "")).isdigit()
            }
            if dimension_by_id:
                by_id = {int(item["id"]): item for item in groups}
                for group_id, dimension in dimension_by_id.items():
                    if group_id not in by_id:
                        by_id[group_id] = {
                            "id": group_id,
                            "name": str(dimension.get("name", "未命名分组")),
                            "models": [],
                        }
                    else:
                        by_id[group_id]["name"] = str(dimension.get("name", by_id[group_id].get("name", "未命名分组")))
                groups = [by_id[group_id] for group_id in dimension_by_id]
        except (aiohttp.ClientError, TimeoutError, ValueError, KeyError):
            pass
        groups.sort(key=lambda item: str(item.get("name", "")).lower())
        self.model_plaza_cache = (now, groups)
        return groups

    @staticmethod
    def _is_monitored_group(name: str) -> bool:
        normalized = re.sub(r"[\s【】\[\]（）()]", "", name).lower()
        normalized = (
            normalized.replace("￥", "¥")
            .replace("／", "/")
            .replace("–", "-")
            .replace("—", "-")
        )
        return any(
            all(fragment.lower() in normalized for fragment in rule)
            for rule in MONITORED_GROUP_RULES
        )

    async def _download_model_status_png(self, group_id: int | None = None, search: str = "") -> bytes:
        base = str(self.config.get("model_status_report_url", "http://model-status-report:8000")).strip().rstrip("/")
        timeout = aiohttp.ClientTimeout(total=max(45, int(self.config.get("request_timeout", 15))))
        async with aiohttp.ClientSession(timeout=timeout) as session:
            params = {"group_id": str(group_id)} if group_id is not None else {}
            if search.strip():
                params["search"] = search.strip()
            async with session.get(f"{base}/api/reports/png", params=params) as response:
                body = await response.read()
                if response.status != 200 or not body.startswith(b"\x89PNG\r\n\x1a\n"):
                    detail = body.decode("utf-8", errors="replace")[:200]
                    raise RuntimeError(f"报告服务返回异常（HTTP {response.status}）：{detail}")
                return body

    @staticmethod
    def _select_visible_group(
        groups: list[dict[str, Any]], selector: str
    ) -> dict[str, Any] | None:
        value = selector.strip()
        if value.isdigit():
            index = int(value) - 1
            if 0 <= index < len(groups):
                return groups[index]
            return None
        lowered = value.lower()
        matches = [
            item for item in groups if lowered in str(item.get("name", "")).lower()
        ]
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _monitor_status_text(status: str) -> str:
        return {
            "healthy": "正常",
            "warning": "波动",
            "critical": "异常",
            "unknown": "状态待确认",
        }.get(status.strip().lower(), "状态待确认")

    @staticmethod
    def _normalized_model_name(name: str) -> str:
        return re.sub(r"\s+", "", name).lower()

    def _match_monitored_model(
        self,
        monitored: list[dict[str, Any]],
        platform: str,
        model_name: str,
    ) -> dict[str, Any] | None:
        exact = next(
            (
                item
                for item in monitored
                if str(item.get("platform", "")).lower() == platform.lower()
                and self._normalized_model_name(str(item.get("model", "")))
                == self._normalized_model_name(model_name)
            ),
            None,
        )
        if exact:
            return exact
        name_matches = [
            item
            for item in monitored
            if self._normalized_model_name(str(item.get("model", "")))
            == self._normalized_model_name(model_name)
        ]
        return name_matches[0] if len(name_matches) == 1 else None

    def _public_monitor_status(
        self, monitored_item: dict[str, Any] | None
    ) -> tuple[str, str]:
        if not monitored_item:
            return "inactive", "近15分钟无调用"
        metrics = monitored_item.get("metrics", {})
        health = monitored_item.get("health", {})
        raw = str(health.get("overall", "")).strip().lower()
        requests = int(metrics.get("request_count", 0) or 0)
        if raw in {"", "unknown"} and requests == 0:
            return "inactive", "近15分钟无调用"
        key = raw if raw in {"healthy", "warning", "critical"} else "unknown"
        return key, self._monitor_status_text(raw)

    @staticmethod
    def _monitor_latency_text(value: Any) -> str:
        if value is None:
            return "-"
        try:
            milliseconds = int(value)
        except (TypeError, ValueError):
            return "-"
        if milliseconds >= 1000:
            return f"{milliseconds / 1000:.1f}s"
        return f"{milliseconds}ms"

    @staticmethod
    def _group_success_rate(monitored: list[dict[str, Any]]) -> float | None:
        successes = 0.0
        requests_total = 0
        for item in monitored:
            metrics = item.get("metrics", {})
            if not isinstance(metrics, dict):
                continue
            try:
                requests = int(metrics.get("request_count", 0) or 0)
                rate = float(metrics.get("success_rate", 0) or 0)
            except (TypeError, ValueError):
                continue
            if requests <= 0:
                continue
            # The monitor API has used both 0-1 and 0-100 representations.
            normalized_rate = rate / 100 if rate > 1 else rate
            successes += requests * max(0.0, min(normalized_rate, 1.0))
            requests_total += requests
        if requests_total == 0:
            return None
        return successes / requests_total * 100

    @filter.command("监控分组")
    async def monitor_groups(self, event: AstrMessageEvent):
        if not self._is_current_instance():
            return
        try:
            groups = await self._model_plaza_groups()
            if not groups:
                yield event.plain_result("目前没有可用的模型监控分组。")
                return
            yield event.plain_result(
                "模型监控分组：\n"
                + "\n".join(
                    f"{index}. {item.get('name', '未命名')}"
                    for index, item in enumerate(groups, 1)
                )
                + "\n\n查看全部状态：/模型状态；查看单组状态：/模型状态 序号"
            )
        except (aiohttp.ClientError, TimeoutError, ValueError, KeyError) as exc:
            logger.warning("monitor group query failed: %s", type(exc).__name__)
            yield event.plain_result(f"模型监控查询失败：{exc}")

    @filter.command("模型状态")
    async def model_status(self, event: AstrMessageEvent, group_name: str = ""):
        if not self._is_current_instance():
            return
        if hasattr(event, "stop_event"):
            event.stop_event()
        try:
            if not group_name.strip():
                raw_text = str(getattr(event, "message_str", "") or "").strip()
                if not raw_text and hasattr(event, "get_message_str"):
                    raw_text = str(event.get_message_str() or "").strip()
                parts = raw_text.replace("／", "/").split()
                if len(parts) >= 2 and parts[0].lstrip("/") == "模型状态":
                    group_name = parts[1]
            groups = await self._model_plaza_groups()
            selector = group_name.strip()
            if not selector:
                image_bytes = await self._download_model_status_png()
                image_dir = self.data_dir / "model_status_reports"
                image_dir.mkdir(parents=True, exist_ok=True)
                image_path = image_dir / f"all-groups-{time.time_ns()}-{uuid.uuid4().hex[:8]}.png"
                image_path.write_bytes(image_bytes)
                yield event.image_result(str(image_path.resolve()))
                return
            group = self._select_visible_group(groups, selector)
            if not group:
                yield event.plain_result("没有找到这个分组，请使用 /监控分组 中的序号。")
                return
            image_bytes = await self._download_model_status_png(int(group["id"]))
            image_dir = self.data_dir / "model_status_reports"
            image_dir.mkdir(parents=True, exist_ok=True)
            # A unique path prevents AstrBot/QQ from reusing a previously sent
            # image when the report content changes but the filename does not.
            image_path = image_dir / (
                f"group-{int(group['id'])}-{time.time_ns()}-{uuid.uuid4().hex[:8]}.png"
            )
            image_path.write_bytes(image_bytes)
            yield event.image_result(str(image_path.resolve()))
        except (aiohttp.ClientError, TimeoutError, ValueError, KeyError, RuntimeError) as exc:
            logger.warning("model status query failed: %s", type(exc).__name__)
            yield event.plain_result(f"模型状态图片获取失败：{exc}")

    @filter.command("模型监控")
    async def model_monitor(self, event: AstrMessageEvent, keyword: str = ""):
        if not self._is_current_instance():
            return
        if hasattr(event, "stop_event"):
            event.stop_event()
        raw_text = str(getattr(event, "message_str", "") or "").strip()
        if not raw_text and hasattr(event, "get_message_str"):
            raw_text = str(event.get_message_str() or "").strip()
        parts = raw_text.replace("／", "/").split(maxsplit=1)
        if len(parts) >= 2 and parts[0].lstrip("/") == "模型监控":
            keyword = parts[1].strip()
        keyword = keyword.strip()
        try:
            image_bytes = await self._download_model_status_png(search=keyword)
            image_dir = self.data_dir / "model_status_reports"
            image_dir.mkdir(parents=True, exist_ok=True)
            prefix = "all-groups" if not keyword else "search"
            image_path = image_dir / f"{prefix}-{time.time_ns()}-{uuid.uuid4().hex[:8]}.png"
            image_path.write_bytes(image_bytes)
            yield event.image_result(str(image_path.resolve()))
        except (aiohttp.ClientError, TimeoutError, ValueError, RuntimeError) as exc:
            logger.warning("model monitor search failed: %s", type(exc).__name__)
            yield event.plain_result(f"模型监控图片获取失败：{exc}")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def welcome_new_member(self, event: AstrMessageEvent):
        if not self._is_current_instance():
            return
        message_obj = getattr(event, "message_obj", None)
        raw = getattr(message_obj, "raw_message", None)
        if not isinstance(raw, dict):
            return
        if raw.get("post_type") != "notice" or raw.get("notice_type") != "group_increase":
            return
        group_id = str(raw.get("group_id", "")).strip()
        if not group_id or not self._welcome_enabled(group_id):
            return
        user_id = str(raw.get("user_id", "")).strip()
        self_id = str(raw.get("self_id", "")).strip()
        if not user_id or user_id == self_id:
            return
        yield event.plain_result("欢迎新朋友加入群聊 👋\n这里可以聊 API 接入、模型选择、报错排查和渠道状态，有问题直接 @我就行。")

    async def _set_group_welcome(self, event: AstrMessageEvent, enabled: bool):
        if not self._is_current_instance():
            return
        group_id = str(event.get_group_id() or "").strip()
        if not self._authorized(event) or not group_id:
            yield event.plain_result("新人欢迎开关仅限管理员在群内操作。")
            return
        self.group_welcome_settings[group_id] = enabled
        self._save_group_welcome_settings()
        status = "开启" if enabled else "关闭"
        yield event.plain_result(f"已为本群{status}新人欢迎语。")

    @filter.command("开启新人欢迎")
    async def enable_group_welcome(self, event: AstrMessageEvent):
        async for result in self._set_group_welcome(event, True):
            yield result

    @filter.command("关闭新人欢迎")
    async def disable_group_welcome(self, event: AstrMessageEvent):
        async for result in self._set_group_welcome(event, False):
            yield result

    @filter.command("新人欢迎状态")
    async def group_welcome_status(self, event: AstrMessageEvent):
        if not self._is_current_instance():
            return
        group_id = str(event.get_group_id() or "").strip()
        if not self._authorized(event) or not group_id:
            yield event.plain_result("新人欢迎状态仅限管理员在群内查看。")
            return
        status = "开启" if self._welcome_enabled(group_id) else "关闭"
        yield event.plain_result(f"本群新人欢迎语当前为：{status}。")

    def _reminder_chain(self):
        return MessageChain([
            Plain(
                "欢迎加入Passion！ 🎉\n\n"
                "❤️ 试吃领取找 @领取试吃找我/发邮箱（新增额度为15刀，折合人民币3元）\n"
                "❤️ 充值及代码问题找群主 @充值/代码问题找我\n"
                "❤️ 酒馆/Airp/小手机/Chatbox问题找 @水（答疑可私没回就是不在）@酒馆/小手机/chatbox\n"
                "❤️ 报错可私 @Airp相关问题可以找我\n\n"
                "❗进群请先看群公告\n\n"
                "1️⃣ 遇到报错私信管理时请带上报错截图，方便排查。\n"
                "2️⃣ 截图位置：网站-使用记录-错误请求，请截图完整\n"
                "3️⃣ 询问报错问题请私信对应管理～"
            )
        ])

    async def _group_reminder_loop(self, group_id: str, origin: str, minutes: int):
        try:
            while True:
                await asyncio.sleep(minutes * 60)
                await self.context.send_message(origin, self._reminder_chain())
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("群提醒发送失败，群号=%s", group_id)

    @filter.command("设置群提醒")
    async def set_group_reminder(self, event: AstrMessageEvent, minutes: str = ""):
        if not self._authorized(event) or not event.get_group_id():
            yield event.plain_result("群提醒仅限管理员在群内设置。")
            return
        try:
            interval = int(minutes)
        except ValueError:
            interval = 0
        if interval < 1:
            yield event.plain_result("用法：/设置群提醒 分钟数，例如 /设置群提醒 30")
            return
        group_id = str(event.get_group_id())
        origin = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if not origin:
            yield event.plain_result("无法确定当前群消息来源，请稍后重试。")
            return
        old = self.group_reminder_tasks.pop(group_id, None)
        if old:
            old.cancel()
        self.group_reminder_configs[group_id] = {"origin": origin, "minutes": interval}
        self._save_group_reminder_configs()
        self.group_reminder_tasks[group_id] = asyncio.create_task(self._group_reminder_loop(group_id, origin, interval))
        try:
            await self.context.send_message(origin, self._reminder_chain())
            yield event.plain_result(f"已设置本群每 {interval} 分钟发送一次提醒。")
        except Exception:
            logger.exception("群提醒试发失败，群号=%s，定时任务仍已启动", group_id)
            yield event.plain_result(
                f"已设置本群每 {interval} 分钟发送一次提醒；本次试发失败，定时任务仍会继续运行。"
            )

    @filter.command("停止群提醒")
    async def stop_group_reminder(self, event: AstrMessageEvent):
        if not self._authorized(event) or not event.get_group_id():
            yield event.plain_result("群提醒仅限管理员在群内控制。")
            return
        task = self.group_reminder_tasks.pop(str(event.get_group_id()), None)
        if task:
            task.cancel()
            self.group_reminder_configs.pop(str(event.get_group_id()), None)
            self._save_group_reminder_configs()
            yield event.plain_result("已停止本群定时提醒。")
        else:
            yield event.plain_result("本群当前没有启用定时提醒。")

    @filter.command("机器人功能")
    async def bot_features(self, event: AstrMessageEvent):
        if not self._is_current_instance():
            return
        yield event.plain_result(
            "我可以帮你：\n"
            "1. @我进行日常聊天、API 接入答疑和报错分析\n"
            "2. /监控分组 查看客户可见模型分组\n"
            "3. /模型状态 查看各渠道近15分钟成功率\n"
            "4. /模型监控 <关键词> 按分组名、模型名或平台搜索状态\n"
            "5. 新成员入群时自动发送欢迎语（管理员可按群开启或关闭）\n"
            "管理员还可以使用充值、退款和兑换码功能。"
        )

    async def _find_user(self, email: str) -> dict[str, Any]:
        query = urlencode(
            {
                "page": 1,
                "page_size": 200,
                "status": "",
                "role": "",
                "search": email,
                "include_subscriptions": "true",
                "sort_by": "email",
                "sort_order": "asc",
                "timezone": "Asia/Shanghai",
            }
        )
        data = await self._admin_api("GET", f"/api/v1/admin/users?{query}")
        items = data.get("items", []) if isinstance(data, dict) else []
        matches = [
            item
            for item in items
            if isinstance(item, dict) and str(item.get("email", "")).lower() == email.lower()
        ]
        if len(matches) != 1:
            raise ValueError(f"邮箱精确匹配数量为 {len(matches)}，已停止操作。")
        return matches[0]

    async def _user_registered(self, email: str) -> bool:
        """Return registration status using an exact, case-insensitive email match."""
        query = urlencode(
            {
                "page": 1,
                "page_size": 200,
                "status": "",
                "role": "",
                "search": email,
                "include_subscriptions": "false",
                "sort_by": "email",
                "sort_order": "asc",
                "timezone": "Asia/Shanghai",
            }
        )
        data = await self._admin_api("GET", f"/api/v1/admin/users?{query}")
        items = data.get("items", []) if isinstance(data, dict) else []
        return any(
            isinstance(item, dict)
            and str(item.get("email", "")).strip().lower() == email
            for item in items
        )

    async def _find_test_credit_record(self, user_id: int) -> dict[str, Any] | None:
        page = 1
        while True:
            data = await self._admin_api(
                "GET",
                f"/api/v1/admin/users/{user_id}/balance-history?"
                f"{urlencode({'page': page, 'page_size': 200, 'type': 'admin_balance'})}",
            )
            items = data.get("items", []) if isinstance(data, dict) else []
            for item in items:
                if not isinstance(item, dict):
                    continue
                try:
                    value = Decimal(str(item.get("value", 0))).quantize(Decimal("0.01"))
                except InvalidOperation:
                    continue
                if value == Decimal("15.00"):
                    return item
            pages = int(data.get("pages", 1)) if isinstance(data, dict) else 1
            if page >= pages or not items:
                return None
            page += 1

    @staticmethod
    async def _send_plain_with_retry(
        event: AstrMessageEvent, message: str
    ) -> bool:
        """Retry only the notification when the reverse WebSocket reconnects."""
        for attempt, delay in enumerate((0, 2, 5, 10, 20), start=1):
            if delay:
                await asyncio.sleep(delay)
            try:
                chain = [Plain(message)]
                if event.get_group_id():
                    chain = [At(qq=str(event.get_sender_id())), Plain(f"\n{message}")]
                    message_obj = getattr(event, "message_obj", None)
                    message_id = getattr(message_obj, "message_id", None)
                    if message_id is not None and str(message_id).strip():
                        chain.insert(0, Reply(id=message_id))
                await event.send(MessageChain(chain))
                return True
            except Exception as exc:
                logger.warning(
                    "QQ reply attempt %d/5 failed: %s",
                    attempt,
                    type(exc).__name__,
                )
        return False

    async def _auto_test_credit(self, event: AstrMessageEvent, email: str):
        if not self._is_current_instance():
            return
        # This is the final safety boundary: slash commands must never issue credit,
        # even if AstrBot invokes the generic event handler before a command handler.
        if self._is_slash_command_event(event):
            return
        if not self._test_credit_authorized(event):
            return
        email = email.strip().lower()
        if not EMAIL_RE.match(email):
            if not await self._send_plain_with_retry(event, "邮箱格式不正确。"):
                yield event.plain_result("邮箱格式不正确。")
            return
        if not self._claim_request(event, "auto_test_credit", email):
            return

        item: dict[str, Any] | None = None
        try:
            user = await self._find_user(email)
            user_id = int(user["id"])
            lock = self.auto_credit_locks.setdefault(user_id, asyncio.Lock())
            async with lock:
                existing = await self._find_test_credit_record(user_id)
                if existing:
                    created_at = str(existing.get("created_at", "")).strip()
                    time_text = f"\n记录时间：{created_at}" if created_at else ""
                    message = (
                        f"无需充值：{email} 已有一笔管理员发放的 $15.00 记录。{time_text}"
                    )
                    if not await self._send_plain_with_retry(event, message):
                        yield event.plain_result(message)
                    return

                amount = Decimal("15.00")
                notes = "测试额度-15刀"
                item = {
                    "operation_id": uuid.uuid4().hex,
                    "qq_id": str(event.get_sender_id()),
                    "action": "add",
                    "email": email,
                    "user_id": user_id,
                    "amount": amount,
                    "notes": notes,
                }
                self._save_operation(item, "executing")
                data = await self._admin_api(
                    "POST",
                    f"/api/v1/admin/users/{user_id}/balance",
                    {"balance": 15.0, "operation": "add", "notes": notes},
                )
                new_balance = data.get("balance") if isinstance(data, dict) else None
                self._save_operation(item, "success", f"new_balance={new_balance}")
                balance_text = (
                    f"\n最新余额：${Decimal(str(new_balance)):.2f}"
                    if new_balance is not None
                    else ""
                )
                message = f"测试额度充值成功\n邮箱：{email}\n金额：$15.00{balance_text}"
                if not await self._send_plain_with_retry(event, message):
                    yield event.plain_result(message)
        except (aiohttp.ClientError, TimeoutError, ValueError, KeyError) as exc:
            if item is not None:
                self._save_operation(item, "failed", str(exc))
            logger.warning("automatic test credit failed: %s", type(exc).__name__)
            message = f"自动检查或充值失败：{exc}"
            if not await self._send_plain_with_retry(event, message):
                yield event.plain_result(message)

    def _parse_amount(self, raw: str) -> Decimal:
        try:
            amount = Decimal(raw).quantize(Decimal("0.01"))
        except InvalidOperation as exc:
            raise ValueError("金额格式错误，例如应填写 15 或 15.00。") from exc
        maximum = Decimal(str(self.config.get("max_amount", 1000)))
        if amount <= 0 or amount > maximum:
            raise ValueError(f"金额必须在 0.01 到 {maximum:.2f} 之间。")
        return amount

    def _save_operation(self, item: dict[str, Any], status: str, result: str = "") -> None:
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                """
                INSERT INTO operations(operation_id, qq_id, action, email, amount, notes, status, result)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(operation_id) DO UPDATE SET
                    status=excluded.status,
                    result=excluded.result,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    item["operation_id"], item["qq_id"], item["action"], item["email"],
                    str(item["amount"]), item["notes"], status, result[:500],
                ),
            )

    def _claim_request(self, event: AstrMessageEvent, action: str, payload: str) -> bool:
        message_obj = getattr(event, "message_obj", None)
        message_id = getattr(message_obj, "message_id", None)
        if message_id is not None and str(message_id).strip():
            source = (
                f"message|{event.get_sender_id()}|{event.get_group_id()}|"
                f"{message_id}|{action}"
            )
            dedupe_window = 7 * 86400
        else:
            source = f"fallback|{event.get_sender_id()}|{action}|{payload.strip().lower()}"
            dedupe_window = 10.0
        fingerprint = hashlib.sha256(source.encode("utf-8")).hexdigest()
        now = time.time()
        with sqlite3.connect(self.db_path) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT claimed_at FROM request_claims WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchone()
            if row and now - float(row[0]) < dedupe_window:
                db.rollback()
                return False
            db.execute(
                """
                INSERT INTO request_claims(fingerprint, claimed_at) VALUES (?, ?)
                ON CONFLICT(fingerprint) DO UPDATE SET claimed_at=excluded.claimed_at
                """,
                (fingerprint, now),
            )
            db.execute("DELETE FROM request_claims WHERE claimed_at < ?", (now - 7 * 86400,))
            db.commit()
            return True

    async def _prepare(
        self, event: AstrMessageEvent, action: str, email: str, amount_raw: str, notes: str
    ):
        if not self._is_current_instance():
            return
        if not self._authorized(event):
            yield event.plain_result("无权操作。充值和退款仅允许指定管理员执行。")
            return
        email = email.strip().lower()
        if not EMAIL_RE.match(email):
            yield event.plain_result("邮箱格式不正确。")
            return
        if not self._claim_request(event, action, f"{email}|{amount_raw}|{notes}"):
            return
        try:
            amount = self._parse_amount(amount_raw)
            user = await self._find_user(email)
            balance = Decimal(str(user.get("balance", 0))).quantize(Decimal("0.01"))
            if action == "subtract" and amount > balance:
                raise ValueError("退款金额不能超过用户当前余额。")
            code = f"{secrets.randbelow(1_000_000):06d}"
            item = {
                "operation_id": uuid.uuid4().hex,
                "qq_id": str(event.get_sender_id()),
                "action": action,
                "email": email,
                "user_id": int(user["id"]),
                "amount": amount,
                "notes": notes.strip()[:200] or ("机器人充值" if action == "add" else "机器人退款"),
                "balance": balance,
                "code": code,
                "expires_at": time.monotonic() + 120,
                "conversation_key": self._conversation_key(event),
            }
            self.pending[item["qq_id"]] = item
            self._save_operation(item, "pending")
            name = "充值" if action == "add" else "退款"
            yield event.plain_result(
                f"{name}预览\n用户 ID：{item['user_id']}\n邮箱：{email}\n"
                f"当前余额：${balance:.2f}\n{name}金额：${amount:.2f}\n备注：{item['notes']}\n\n"
                "请在 2 分钟内回复：确认\n"
                "取消操作请回复：取消\n"
                f"也可使用：/确认操作 {code}"
            )
        except (aiohttp.ClientError, TimeoutError, ValueError, KeyError) as exc:
            logger.warning("passion prepare failed: %s", type(exc).__name__)
            yield event.plain_result(f"预览失败：{exc}")

    async def _generate_redeem_code(
        self, event: AstrMessageEvent, amount_raw: str, count_raw: str = "1"
    ):
        if not self._is_current_instance():
            return
        if not self._private_authorized(event):
            yield event.plain_result("无权操作。兑换码仅允许指定管理员私聊生成。")
            return
        item = {
            "operation_id": uuid.uuid4().hex,
            "qq_id": str(event.get_sender_id()),
            "action": "redeem",
            "email": "-",
            "amount": Decimal("0"),
            "notes": "生成余额兑换码",
        }
        try:
            amount = self._parse_amount(amount_raw)
            try:
                count = int(count_raw)
            except ValueError as exc:
                raise ValueError("兑换码数量必须是整数。") from exc
            max_count = int(self.config.get("max_code_count", 20))
            if count < 1 or count > max_count:
                raise ValueError(f"兑换码数量必须在 1 到 {max_count} 之间。")
            if not self._claim_request(event, "redeem", f"{amount}|{count}"):
                return
            item["amount"] = amount
            item["notes"] = f"生成 {count} 张余额兑换码"
            self._save_operation(item, "executing")
            data = await self._admin_api(
                "POST",
                "/api/v1/admin/redeem-codes/generate",
                {"count": count, "type": "balance", "value": float(amount)},
            )
            codes = [
                str(entry.get("code", "")).strip()
                for entry in (data if isinstance(data, list) else [])
                if isinstance(entry, dict) and entry.get("code")
            ]
            if len(codes) != count:
                raise ValueError(f"接口返回兑换码数量为 {len(codes)}，已停止显示。")
            self._save_operation(item, "success", f"generated={count}")
            code_lines = "\n".join(f"{index}. {code}" for index, code in enumerate(codes, 1))
            yield event.plain_result(
                f"兑换码生成成功\n单张额度：${amount:.2f}\n数量：{count}\n"
                f"有效期：永不过期\n兑换码：\n{code_lines}"
            )
        except (aiohttp.ClientError, TimeoutError, ValueError, KeyError) as exc:
            self._save_operation(item, "failed", str(exc))
            logger.warning("redeem code generation failed: %s", type(exc).__name__)
            yield event.plain_result(f"兑换码生成失败：{exc}")

    @filter.command("兑换码")
    async def redeem_code(
        self, event: AstrMessageEvent, amount: str = "", count: str = "1"
    ):
        if not amount:
            yield event.plain_result("用法：/兑换码 <金额> [数量]，例如 /兑换码 15 2")
            return
        async for result in self._generate_redeem_code(event, amount, count):
            yield result

    @filter.command("充值")
    async def recharge(
        self, event: AstrMessageEvent, email: str = "", amount: str = "15.00", notes: str = "测试额度-15刀"
    ):
        async for result in self._prepare(event, "add", email, amount, notes):
            yield result

    @filter.command("用户")
    async def check_user(self, event: AstrMessageEvent, email: str = ""):
        """Check whether one exact email has a registered website account."""
        if not self._is_current_instance():
            return
        # Prevent the catch-all email handler from processing this command again.
        if hasattr(event, "stop_event"):
            event.stop_event()
        if not self._test_credit_authorized(event):
            yield event.plain_result("无权操作。用户查询仅限管理员使用。")
            return
        email = email.strip().lower()
        if not EMAIL_RE.fullmatch(email):
            yield event.plain_result("用法：/用户 <邮箱>")
            return
        try:
            registered = await self._user_registered(email)
        except (aiohttp.ClientError, TimeoutError, ValueError, KeyError) as exc:
            logger.warning("user registration lookup failed: %s", type(exc).__name__)
            yield event.plain_result("用户查询失败，请稍后重试。")
            return
        yield event.plain_result("已注册用户" if registered else "未注册用户")

    @filter.command("余额")
    async def check_balance(self, event: AstrMessageEvent, email: str = ""):
        """Return the current balance for one exact website account."""
        if not self._is_current_instance():
            return
        if hasattr(event, "stop_event"):
            event.stop_event()
        if not self._test_credit_authorized(event):
            yield event.plain_result("无权操作。余额查询仅限管理员使用。")
            return
        email = email.strip().lower()
        if not self._valid_account_email(email):
            yield event.plain_result("邮箱格式错误")
            return
        try:
            if not await self._user_registered(email):
                yield event.plain_result("未注册用户")
                return
            user = await self._find_user(email)
            balance = Decimal(str(user.get("balance", 0))).quantize(Decimal("0.01"))
        except InvalidOperation:
            logger.warning("balance lookup returned an invalid balance")
            yield event.plain_result("余额查询失败，请稍后重试。")
            return
        except (aiohttp.ClientError, TimeoutError, ValueError, KeyError) as exc:
            logger.warning("balance lookup failed: %s", type(exc).__name__)
            yield event.plain_result("余额查询失败，请稍后重试。")
            return
        yield event.plain_result(f"账号余额：${balance:.2f}")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def recognize_customer_message(self, event: AstrMessageEvent):
        if not self._is_current_instance():
            return
        if self._is_slash_command_event(event):
            return
        # 普通群成员必须 @ 机器人；管理员可直接粘贴邮箱执行管理操作。
        if not self._is_mentioned_in_group(event) and not self._test_credit_authorized(event):
            return
        if not self._test_credit_authorized(event):
            return
        text = str(getattr(event, "message_str", "") or "").strip()
        if not text and hasattr(event, "get_message_str"):
            text = str(event.get_message_str() or "").strip()
        if not text:
            return

        if text in {"确认", "确定"} and self._authorized(event):
            if hasattr(event, "stop_event"):
                event.stop_event()
            async for result in self._confirm_pending(event):
                yield result
            return

        if text == "取消" and self._authorized(event):
            if hasattr(event, "stop_event"):
                event.stop_event()
            async for result in self._cancel_pending(event):
                yield result
            return

        for pattern in REDEEM_REQUEST_PATTERNS if self._authorized(event) else ():
            match = pattern.fullmatch(text)
            if match:
                if hasattr(event, "stop_event"):
                    event.stop_event()
                async for result in self._generate_redeem_code(
                    event, match.group("amount"), match.group("count") or "1"
                ):
                    yield result
                return

        emails = sorted({match.group(0).lower() for match in EMAIL_IN_TEXT_RE.finditer(text)})
        if not emails:
            return
        if hasattr(event, "stop_event"):
            event.stop_event()
        if len(emails) != 1:
            yield event.plain_result("消息中发现多个不同邮箱，已停止操作。请一次只粘贴一位顾客的消息。")
            return

        async for result in self._auto_test_credit(event, emails[0]):
            yield result

    @filter.command("退款")
    async def refund(
        self, event: AstrMessageEvent, email: str = "", amount: str = "15.00", notes: str = "测试额度-15刀"
    ):
        async for result in self._prepare(event, "subtract", email, amount, notes):
            yield result

    @filter.command("确认操作")
    async def confirm(self, event: AstrMessageEvent, code: str = ""):
        async for result in self._confirm_pending(event, code):
            yield result

    async def _cancel_pending(self, event: AstrMessageEvent):
        if not self._is_current_instance():
            return
        if not self._authorized(event):
            yield event.plain_result("无权操作。")
            return
        qq_id = str(event.get_sender_id())
        item = self.pending.get(qq_id)
        if not item or time.monotonic() > item["expires_at"]:
            self.pending.pop(qq_id, None)
            yield event.plain_result("没有待确认操作，或确认码已过期。")
            return
        if item.get("conversation_key") != self._conversation_key(event):
            yield event.plain_result("请在生成预览的同一私聊或群聊中取消。")
            return
        if not self._claim_request(event, "cancel", item["operation_id"]):
            return
        self.pending.pop(qq_id, None)
        self._save_operation(item, "cancelled")
        yield event.plain_result("操作已取消。")

    async def _confirm_pending(self, event: AstrMessageEvent, code: str | None = None):
        if not self._is_current_instance():
            return
        if not self._authorized(event):
            yield event.plain_result("无权操作。")
            return
        qq_id = str(event.get_sender_id())
        item = self.pending.get(qq_id)
        if not item or time.monotonic() > item["expires_at"]:
            self.pending.pop(qq_id, None)
            yield event.plain_result("没有待确认操作，或确认码已过期。")
            return
        if item.get("conversation_key") != self._conversation_key(event):
            yield event.plain_result("请在生成预览的同一私聊或群聊中确认。")
            return
        if code is not None and not secrets.compare_digest(item["code"], code.strip()):
            yield event.plain_result("确认码不正确。")
            return
        if not self._claim_request(event, "confirm", item["operation_id"]):
            return

        self.pending.pop(qq_id, None)
        self._save_operation(item, "executing")
        try:
            data = await self._admin_api(
                "POST",
                f"/api/v1/admin/users/{item['user_id']}/balance",
                {
                    "balance": float(item["amount"]),
                    "operation": item["action"],
                    "notes": item["notes"],
                },
            )
            new_balance = data.get("balance") if isinstance(data, dict) else None
            result = f"new_balance={new_balance}"
            self._save_operation(item, "success", result)
            name = "充值" if item["action"] == "add" else "退款"
            balance_text = f"\n最新余额：${Decimal(str(new_balance)):.2f}" if new_balance is not None else ""
            yield event.plain_result(
                f"{name}成功\n邮箱：{item['email']}\n金额：${item['amount']:.2f}{balance_text}"
            )
        except (aiohttp.ClientError, TimeoutError, ValueError, KeyError) as exc:
            self._save_operation(item, "failed", str(exc))
            logger.warning("passion operation failed: %s", type(exc).__name__)
            yield event.plain_result(f"操作失败：{exc}")

    @filter.command("充值帮助")
    async def help(self, event: AstrMessageEvent):
        if not self._is_current_instance():
            return
        if not self._authorized(event):
            return
        yield event.plain_result(
            "管理员余额操作\n"
            "直接发送或粘贴唯一邮箱：自动检查是否有管理员发放的 $15 记录；没有则直接充值，已有则提示。\n"
            "/兑换码 <金额> [数量]（例如 /兑换码 15 2）\n"
            "也可以直接发送：生成2张15刀兑换码\n"
            "/充值 <邮箱> [金额] [备注]\n"
            "/退款 <邮箱> [金额] [备注]\n"
            "/余额 <邮箱>：查询指定账号的当前余额（普通管理员可用）\n"
            "预览后直接回复：确认；放弃请回复：取消\n"
            "/确认操作 <六位确认码>（兼容旧方式）\n"
            "默认金额为 15.00，预览 2 分钟有效，充值和退款支持管理员私聊或群聊。\n"
            "兑换码仅限管理员私聊生成。"
            "\n/监控分组：查看模型监控分组。"
            "\n/模型状态：查看各渠道近15分钟成功率；也可加分组序号单独查询。"
            "\n/模型监控 <关键词>：按分组名、模型名或平台搜索状态图片。"
            "\n/机器人功能：查看群助手功能。"
        )
