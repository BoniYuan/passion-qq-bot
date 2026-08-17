import json
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import aiohttp
from cryptography.fernet import Fernet, InvalidToken

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


@register("astrbot_plugin_sub2", "local", "Sub2 中转站签到与额度查询", "0.1.0")
class Sub2Plugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_dir = Path("data/plugin_data/astrbot_plugin_sub2")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "bindings.db"
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS bindings (
                    user_id TEXT PRIMARY KEY,
                    encrypted_token TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    def _fernet(self) -> Fernet:
        key = str(self.config.get("encryption_key", "")).strip()
        if not key:
            raise ValueError("管理员尚未配置 encryption_key")
        try:
            return Fernet(key.encode("ascii"))
        except (ValueError, UnicodeError) as exc:
            raise ValueError("encryption_key 格式无效，请重新生成") from exc

    @staticmethod
    def _is_private(event: AstrMessageEvent) -> bool:
        return not bool(event.get_group_id())

    def _save_token(self, user_id: str, token: str) -> None:
        encrypted = self._fernet().encrypt(token.encode("utf-8")).decode("ascii")
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                """
                INSERT INTO bindings(user_id, encrypted_token, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO UPDATE SET
                    encrypted_token=excluded.encrypted_token,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (user_id, encrypted),
            )

    def _load_token(self, user_id: str) -> str | None:
        with sqlite3.connect(self.db_path) as db:
            row = db.execute(
                "SELECT encrypted_token FROM bindings WHERE user_id = ?", (user_id,)
            ).fetchone()
        if not row:
            return None
        try:
            return self._fernet().decrypt(row[0].encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("绑定数据无法解密，请私聊执行 /sub2解绑 后重新绑定") from exc

    def _delete_token(self, user_id: str) -> None:
        with sqlite3.connect(self.db_path) as db:
            db.execute("DELETE FROM bindings WHERE user_id = ?", (user_id,))

    @staticmethod
    def _value_at(payload: Any, path: str, default: Any = None) -> Any:
        if not path:
            return default
        current = payload
        for segment in path.split("."):
            if isinstance(current, dict) and segment in current:
                current = current[segment]
            elif isinstance(current, list) and segment.isdigit():
                index = int(segment)
                if index >= len(current):
                    return default
                current = current[index]
            else:
                return default
        return current

    def _auth(self, token: str) -> tuple[dict[str, str], dict[str, str]]:
        mode = str(self.config.get("auth_mode", "bearer")).lower()
        name = str(self.config.get("auth_name", "Authorization"))
        if mode == "cookie":
            return {}, {name: token}
        if mode == "header":
            return {name: token}, {}
        return {"Authorization": f"Bearer {token}"}, {}

    async def _request(self, endpoint: str, method: str, token: str) -> tuple[int, Any]:
        base_url = str(self.config.get("base_url", "")).strip().rstrip("/") + "/"
        if base_url == "/":
            raise ValueError("管理员尚未配置 sub2 base_url")
        url = urljoin(base_url, endpoint.lstrip("/"))
        headers, cookies = self._auth(token)
        headers.setdefault("User-Agent", "AstrBot-Sub2/0.1 (+https://github.com/AstrBotDevs/AstrBot)")
        headers.setdefault("Accept", "application/json")
        timeout = aiohttp.ClientTimeout(total=int(self.config.get("request_timeout", 15)))
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(method.upper(), url, headers=headers, cookies=cookies) as response:
                text = await response.text()
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    payload = {"message": text[:300]}
                return response.status, payload

    def _success(self, status: int, payload: Any) -> bool:
        if not 200 <= status < 300:
            return False
        path = str(self.config.get("success_path", "success")).strip()
        if not path:
            return True
        value = self._value_at(payload, path, True)
        return value is True or value == 1 or str(value).lower() in {"true", "success", "ok"}

    def _message(self, payload: Any, fallback: str) -> str:
        path = str(self.config.get("message_path", "message"))
        value = self._value_at(payload, path, fallback)
        return str(value)[:300]

    def _format_amount(self, value: Any) -> str:
        number = float(value)
        divisor = float(self.config.get("quota_divisor", 1) or 1)
        currency = str(self.config.get("currency", "CNY"))
        return f"{number / divisor:.2f} {currency}"

    @filter.command("sub2帮助")
    async def help(self, event: AstrMessageEvent):
        yield event.plain_result(
            "Sub2 助手\n"
            "/sub2绑定 <令牌>（仅私聊）\n"
            "/sub2额度\n"
            "/sub2签到\n"
            "/sub2解绑（仅私聊）"
        )

    @filter.command("sub2绑定")
    async def bind(self, event: AstrMessageEvent, token: str = ""):
        if not self._is_private(event):
            yield event.plain_result("为防止令牌泄露，请私聊机器人执行绑定。")
            return
        token = token.strip()
        if not token:
            yield event.plain_result("用法：/sub2绑定 <你的访问令牌>")
            return
        try:
            self._save_token(event.get_sender_id(), token)
            yield event.plain_result("绑定成功。令牌已加密保存，现在可以执行 /sub2额度。")
        except ValueError as exc:
            yield event.plain_result(str(exc))

    @filter.command("sub2解绑")
    async def unbind(self, event: AstrMessageEvent):
        if not self._is_private(event):
            yield event.plain_result("请私聊机器人执行解绑。")
            return
        self._delete_token(event.get_sender_id())
        yield event.plain_result("已删除你的 sub2 绑定。")

    @filter.command("sub2签到")
    async def checkin(self, event: AstrMessageEvent):
        try:
            endpoint = str(self.config.get("checkin_endpoint", "")).strip()
            if not endpoint:
                yield event.plain_result(
                    "Passion API 的签到接口只接受网页登录令牌，不接受 API Key。"
                    "为保护账号密码，请在网站控制台右上角手动签到。"
                )
                return
            token = self._load_token(event.get_sender_id())
            if not token:
                yield event.plain_result("尚未绑定，请私聊机器人执行 /sub2绑定 <令牌>。")
                return
            status, payload = await self._request(
                endpoint,
                str(self.config.get("checkin_method", "POST")),
                token,
            )
            message = self._message(payload, "签到成功" if self._success(status, payload) else "签到失败")
            yield event.plain_result(message)
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            logger.warning("sub2 check-in failed: %s", type(exc).__name__)
            yield event.plain_result(f"签到失败：{exc}")

    @filter.command("sub2额度")
    async def balance(self, event: AstrMessageEvent):
        try:
            token = self._load_token(event.get_sender_id())
            if not token:
                yield event.plain_result("尚未绑定，请私聊机器人执行 /sub2绑定 <令牌>。")
                return
            status, payload = await self._request(
                str(self.config.get("balance_endpoint", "/api/user/self")),
                str(self.config.get("balance_method", "GET")),
                token,
            )
            if not self._success(status, payload):
                yield event.plain_result("查询失败：" + self._message(payload, f"HTTP {status}"))
                return

            total = self._value_at(payload, str(self.config.get("balance_total_path", "data.quota")))
            used = self._value_at(payload, str(self.config.get("balance_used_path", "data.used_quota")))
            remaining = self._value_at(payload, str(self.config.get("balance_remaining_path", "")))
            if remaining is None and total is not None and used is not None:
                remaining = float(total) - float(used)
            if total is None and remaining is None:
                yield event.plain_result("接口调用成功，但未找到额度字段，请管理员检查插件 JSON 路径配置。")
                return

            lines = ["Sub2 额度"]
            if total is not None:
                lines.append("总额度：" + self._format_amount(total))
            if used is not None:
                lines.append("已使用：" + self._format_amount(used))
            if remaining is not None:
                lines.append("剩余额度：" + self._format_amount(remaining))
            yield event.plain_result("\n".join(lines))
        except (aiohttp.ClientError, TimeoutError, ValueError, TypeError) as exc:
            logger.warning("sub2 balance query failed: %s", type(exc).__name__)
            yield event.plain_result(f"额度查询失败：{exc}")
