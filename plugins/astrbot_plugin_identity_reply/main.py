import re
import sqlite3
from pathlib import Path
from typing import Any

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


IDENTITY_QUESTIONS = {
    "我是谁",
    "我是谁？",
    "你知道我是谁吗",
    "你知道我是谁吗？",
}
ALIAS_PATTERNS = (
    re.compile(r"(?:以后|今后)(?:请)?(?:就)?(?:叫|喊|称呼)我(?:为|作|叫)?\s*(?P<alias>.+)$"),
    re.compile(r"(?:请)?称呼我(?:为|作|叫)?\s*(?P<alias>.+)$"),
    re.compile(r"把我的(?:昵称|称呼|名字)(?:改成|改为|设成|设置为)\s*(?P<alias>.+)$"),
    re.compile(r"我的新?(?:昵称|称呼)(?:是|叫|改成|改为)\s*(?P<alias>.+)$"),
    re.compile(r"我(?:改名|改昵称)(?:叫|成|为)\s*(?P<alias>.+)$"),
)
FORGET_PHRASES = {"忘记我的称呼", "忘掉我的称呼", "清除我的称呼", "恢复默认称呼"}


@register("astrbot_plugin_identity_reply", "local", "群成员昵称识别", "0.2.0")
class IdentityReplyPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        data_dir = Path("data/plugin_data/astrbot_plugin_identity_reply")
        data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = data_dir / "identities.db"
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS member_aliases (
                    qq_id TEXT PRIMARY KEY,
                    alias TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    @staticmethod
    def _sender_name(event: AstrMessageEvent) -> str:
        message_obj: Any = getattr(event, "message_obj", None)
        raw_message: Any = getattr(message_obj, "raw_message", None)
        if isinstance(raw_message, dict):
            raw_sender = raw_message.get("sender", {})
            if isinstance(raw_sender, dict):
                for key in ("card", "nickname"):
                    value = str(raw_sender.get(key, "") or "").strip()
                    if value:
                        return value

        sender: Any = getattr(message_obj, "sender", None)
        if isinstance(sender, dict):
            for key in ("card", "nickname", "name"):
                value = str(sender.get(key, "") or "").strip()
                if value:
                    return value
        elif sender is not None:
            for key in ("card", "nickname", "name"):
                value = str(getattr(sender, key, "") or "").strip()
                if value:
                    return value

        getter = getattr(event, "get_sender_name", None)
        if callable(getter):
            value = str(getter() or "").strip()
            if value:
                return value

        return str(event.get_sender_id())

    def _saved_alias(self, qq_id: str) -> str | None:
        with sqlite3.connect(self.db_path) as db:
            row = db.execute(
                "SELECT alias FROM member_aliases WHERE qq_id = ?", (qq_id,)
            ).fetchone()
        return str(row[0]) if row else None

    def _save_alias(self, qq_id: str, alias: str) -> None:
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                """
                INSERT INTO member_aliases(qq_id, alias) VALUES (?, ?)
                ON CONFLICT(qq_id) DO UPDATE SET
                    alias=excluded.alias,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (qq_id, alias),
            )

    def _delete_alias(self, qq_id: str) -> None:
        with sqlite3.connect(self.db_path) as db:
            db.execute("DELETE FROM member_aliases WHERE qq_id = ?", (qq_id,))

    @staticmethod
    def _message_text(event: AstrMessageEvent) -> str:
        text = str(getattr(event, "message_str", "") or "").strip()
        if not text and hasattr(event, "get_message_str"):
            text = str(event.get_message_str() or "").strip()
        return text

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def answer_identity(self, event: AstrMessageEvent):
        text = self._message_text(event)
        qq_id = str(event.get_sender_id())
        normalized = text.rstrip("？?").strip()

        for pattern in ALIAS_PATTERNS:
            match = pattern.search(normalized)
            if not match:
                continue
            alias = match.group("alias").strip().strip("。.!！？?")
            if not alias or len(alias) > 24 or "@" in alias:
                if hasattr(event, "stop_event"):
                    event.stop_event()
                yield event.plain_result("这个称呼不太合适，请换一个 24 字以内的简短称呼。")
                return
            self._save_alias(qq_id, alias)
            if hasattr(event, "stop_event"):
                event.stop_event()
            yield event.plain_result(f"好，以后就叫你 {alias}。")
            return

        if normalized in FORGET_PHRASES:
            self._delete_alias(qq_id)
            if hasattr(event, "stop_event"):
                event.stop_event()
            yield event.plain_result(f"好，恢复使用你当前的 QQ 昵称：{self._sender_name(event)}。")
            return

        if text not in IDENTITY_QUESTIONS and not normalized.endswith("我是谁"):
            return
        if hasattr(event, "stop_event"):
            event.stop_event()
        name = self._saved_alias(qq_id) or self._sender_name(event)
        yield event.plain_result(f"当然记得，你是 {name}。")
