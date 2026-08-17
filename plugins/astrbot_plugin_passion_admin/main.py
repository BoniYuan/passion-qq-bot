import asyncio
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
    r"[/／]\s*(?:监控分组|模型状态详情|模型状态|查询额度|机器人功能|"
    r"确认操作|充值帮助|兑换码|充值|退款)(?:\s|$)"
)


@register("astrbot_plugin_passion_admin", "local", "Passion 群聊管理助手", "0.7.5")
class PassionAdminPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.pending: dict[str, dict[str, Any]] = {}
        self.auto_credit_locks: dict[int, asyncio.Lock] = {}
        self.monitor_dimensions_cache: tuple[float, dict[str, Any]] | None = None
        self.monitor_models_cache: dict[int, tuple[float, list[dict[str, Any]]]] = {}
        self.model_plaza_cache: tuple[float, list[dict[str, Any]]] | None = None
        self.admin_token: str | None = None
        self.data_dir = Path("data/plugin_data/astrbot_plugin_passion_admin")
        self.data_dir.mkdir(parents=True, exist_ok=True)
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

    def _authorized(self, event: AstrMessageEvent) -> bool:
        return str(event.get_sender_id()) in self._admin_ids()

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

    async def _monitor_dimensions(self) -> dict[str, Any]:
        now = time.monotonic()
        if (
            self.monitor_dimensions_cache
            and now - self.monitor_dimensions_cache[0] < 30
        ):
            return self.monitor_dimensions_cache[1]
        data = await self._admin_api(
            "GET", "/api/v1/admin/channel-monitor-v2/dimensions?range=90m"
        )
        dimensions = data if isinstance(data, dict) else {}
        self.monitor_dimensions_cache = (now, dimensions)
        return dimensions

    async def _monitor_models(self, group_id: int) -> list[dict[str, Any]]:
        now = time.monotonic()
        cached = self.monitor_models_cache.get(group_id)
        if cached and now - cached[0] < 30:
            return cached[1]
        query = urlencode({"range": "90m", "group_id": group_id})
        data = await self._admin_api(
            "GET", f"/api/v1/admin/channel-monitor-v2/models?{query}"
        )
        items = data.get("items", []) if isinstance(data, dict) else []
        models = [item for item in items if isinstance(item, dict)]
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
            and self._is_monitored_group(str(item.get("name", "")))
        ]
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
            return "inactive", "近90分钟无调用"
        metrics = monitored_item.get("metrics", {})
        health = monitored_item.get("health", {})
        raw = str(health.get("overall", "")).strip().lower()
        requests = int(metrics.get("request_count", 0) or 0)
        if raw in {"", "unknown"} and requests == 0:
            return "inactive", "近90分钟无调用"
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

    @filter.command("监控分组")
    async def monitor_groups(self, event: AstrMessageEvent):
        if not self._is_current_instance():
            return
        if not self._authorized(event):
            yield event.plain_result("模型监控仅限管理员使用。")
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
                + "\n\n查看状态：/模型状态 序号"
            )
        except (aiohttp.ClientError, TimeoutError, ValueError, KeyError) as exc:
            logger.warning("monitor group query failed: %s", type(exc).__name__)
            yield event.plain_result(f"模型监控查询失败：{exc}")

    @filter.command("模型状态")
    async def model_status(self, event: AstrMessageEvent, group_name: str = ""):
        if not self._is_current_instance():
            return
        if not self._authorized(event):
            yield event.plain_result("模型监控仅限管理员使用。")
            return
        try:
            groups = await self._model_plaza_groups()
            if not group_name.strip():
                yield event.plain_result("请先发送 /监控分组，再用 /模型状态 序号 查询。")
                return
            group = self._select_visible_group(groups, group_name)
            if not group:
                yield event.plain_result("没有找到这个分组，请使用 /监控分组 中的序号。")
                return
            monitored = await self._monitor_models(int(group["id"]))
            listed_models = [
                item for item in group.get("models", []) if isinstance(item, dict)
            ]
            statuses: dict[str, list[str]] = {
                "critical": [],
                "warning": [],
                "healthy": [],
                "unknown": [],
                "inactive": [],
            }
            for listed in sorted(
                listed_models, key=lambda item: str(item.get("name", "")).lower()
            ):
                name = str(listed.get("name", "未知模型"))
                platform = str(listed.get("platform", group.get("platform", "")))
                monitored_item = self._match_monitored_model(monitored, platform, name)
                key, _ = self._public_monitor_status(monitored_item)
                statuses[key].append(name)
            lines = [
                f"{group.get('name', '未命名')} 模型状态（近90分钟）",
                f"正常 {len(statuses['healthy'])} | 波动 {len(statuses['warning'])} | "
                f"异常 {len(statuses['critical'])} | 待确认 {len(statuses['unknown'])} | "
                f"无调用 {len(statuses['inactive'])}",
            ]
            if statuses["critical"]:
                lines.append("\n异常：\n" + "\n".join(statuses["critical"]))
            if statuses["warning"]:
                lines.append("\n波动：\n" + "\n".join(statuses["warning"]))
            if statuses["unknown"]:
                lines.append("\n待确认：\n" + "\n".join(statuses["unknown"]))
            if statuses["healthy"]:
                lines.append("\n正常：\n" + "、".join(statuses["healthy"]))
            if statuses["inactive"]:
                lines.append(
                    "\n近90分钟无调用（不代表不可用）：\n"
                    + "、".join(statuses["inactive"])
                )
            if statuses["critical"]:
                lines.append("\n建议暂时避开异常模型，优先选择状态正常的模型。")
            if not listed_models:
                lines.append("该分组目前没有上架模型。")
            yield event.plain_result("\n".join(lines))
        except (aiohttp.ClientError, TimeoutError, ValueError, KeyError) as exc:
            logger.warning("model status query failed: %s", type(exc).__name__)
            yield event.plain_result(f"模型状态查询失败：{exc}")

    @filter.command("模型状态详情")
    async def model_status_detail(self, event: AstrMessageEvent, group_name: str = ""):
        if not self._is_current_instance():
            return
        if not self._authorized(event):
            yield event.plain_result("这个功能仅限管理员使用。")
            return
        try:
            groups = await self._model_plaza_groups()
            group = self._select_visible_group(groups, group_name)
            if not group:
                yield event.plain_result("用法：/模型状态详情 分组序号")
                return
            monitored = await self._monitor_models(int(group["id"]))
            lines = [f"{group.get('name', '未命名')} 内部监控详情（近90分钟）："]
            listed_models = [
                item for item in group.get("models", []) if isinstance(item, dict)
            ]
            for listed in sorted(
                listed_models, key=lambda item: str(item.get("name", "")).lower()
            ):
                name = str(listed.get("name", "未知模型"))
                platform = str(listed.get("platform", group.get("platform", "")))
                monitored_item = self._match_monitored_model(monitored, platform, name) or {}
                metrics = monitored_item.get("metrics", {})
                health = monitored_item.get("health", {})
                _, status = self._public_monitor_status(monitored_item or None)
                requests = int(metrics.get("request_count", 0) or 0)
                success = float(metrics.get("success_rate", 0) or 0) * 100
                latency = self._monitor_latency_text(
                    metrics.get("ttft", {}).get("p50_ms")
                )
                lines.append(
                    f"{name} | {status} | 成功 {success:.1f}% | "
                    f"首Token {latency} | 请求 {requests}"
                )
            yield event.plain_result("\n".join(lines))
        except (aiohttp.ClientError, TimeoutError, ValueError, KeyError) as exc:
            logger.warning("model detail query failed: %s", type(exc).__name__)
            yield event.plain_result(f"模型状态详情查询失败：{exc}")

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
        user_id = str(raw.get("user_id", "")).strip()
        self_id = str(raw.get("self_id", "")).strip()
        if not user_id or user_id == self_id:
            return
        yield event.plain_result(
            "欢迎新朋友加入群聊 👋\n"
            "这里可以聊 API 接入、模型选择、报错排查和渠道状态，有问题直接 @我就行。"
        )

    @filter.command("机器人功能")
    async def bot_features(self, event: AstrMessageEvent):
        if not self._is_current_instance():
            return
        yield event.plain_result(
            "我可以帮你：\n"
            "1. @我进行日常聊天、API 接入答疑和报错分析\n"
            "2. /监控分组 查看客户可见模型分组\n"
            "3. /模型状态 分组序号 查看已上架模型状态\n"
            "4. 新成员入群时自动发送欢迎语\n"
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

    async def _auto_test_credit(self, event: AstrMessageEvent, email: str):
        if not self._is_current_instance():
            return
        if not self._authorized(event):
            return
        email = email.strip().lower()
        if not EMAIL_RE.match(email):
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
                    yield event.plain_result(
                        f"无需充值：{email} 已有一笔管理员发放的 $15.00 记录。{time_text}"
                    )
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
                yield event.plain_result(
                    f"测试额度充值成功\n邮箱：{email}\n金额：$15.00{balance_text}"
                )
        except (aiohttp.ClientError, TimeoutError, ValueError, KeyError) as exc:
            if item is not None:
                self._save_operation(item, "failed", str(exc))
            logger.warning("automatic test credit failed: %s", type(exc).__name__)
            yield event.plain_result(f"自动检查或充值失败：{exc}")

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

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def recognize_customer_message(self, event: AstrMessageEvent):
        if not self._is_current_instance():
            return
        # 普通群成员必须 @ 机器人；管理员可直接粘贴邮箱执行管理操作。
        if not self._is_mentioned_in_group(event) and not self._authorized(event):
            return
        if not self._authorized(event):
            return
        text = str(getattr(event, "message_str", "") or "").strip()
        if not text and hasattr(event, "get_message_str"):
            text = str(event.get_message_str() or "").strip()
        if not text or text.startswith(("/", "／")) or ADMIN_COMMAND_RE.search(text):
            return

        if text in {"确认", "确定"}:
            if hasattr(event, "stop_event"):
                event.stop_event()
            async for result in self._confirm_pending(event):
                yield result
            return

        if text == "取消":
            if hasattr(event, "stop_event"):
                event.stop_event()
            async for result in self._cancel_pending(event):
                yield result
            return

        for pattern in REDEEM_REQUEST_PATTERNS:
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
            "预览后直接回复：确认；放弃请回复：取消\n"
            "/确认操作 <六位确认码>（兼容旧方式）\n"
            "默认金额为 15.00，预览 2 分钟有效，充值和退款支持管理员私聊或群聊。\n"
            "兑换码仅限管理员私聊生成。"
            "\n/监控分组：查看模型监控分组。"
            "\n/模型状态 <分组序号>：查看已上架模型的简化状态。"
            "\n/模型状态详情 <分组序号>：管理员查看内部监控指标。"
            "\n/机器人功能：查看群助手功能。"
        )
