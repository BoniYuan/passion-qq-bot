import asyncio
import json
import re
import urllib.request
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


@register("astrbot_plugin_passion_faq", "local", "Passion 客户 FAQ 固定回复", "0.4.6")
class PassionFaqPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.faq_path = Path(__file__).with_name("faq.json")
        self._mtime = -1.0
        self._entries: list[dict[str, Any]] = []
        self._semantic_vectors: list[tuple[list[float], str, str]] = []
        self._semantic_lock = asyncio.Lock()
        self._blocked_terms = self._load_blocked_terms()
        self._load_entries()
        try:
            asyncio.get_running_loop().create_task(self._ensure_semantic_vectors())
        except RuntimeError:
            pass

    def _load_blocked_terms(self) -> list[str]:
        path = Path(__file__).with_name("sensitive_words.json")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return [str(item).strip().lower() for item in data if str(item).strip()]
        except (OSError, json.JSONDecodeError, TypeError):
            logger.warning("Passion FAQ sensitive word list unavailable")
            return []

    def _blocked_term(self, text: str) -> str | None:
        normalized = re.sub(r"\s+", "", text).lower()
        direct = next((term for term in self._blocked_terms if term in normalized), None)
        if direct:
            return direct
        if re.search(
            r"(?:叫|喊)(?:我|你)?(?:爸爸|妈妈|爷爷|奶奶|哥哥|姐姐|弟弟|妹妹|主人|老公|老婆|宝贝)",
            normalized,
        ):
            return "称呼调教"
        return None

    def _block_message(self, event: AstrMessageEvent):
        result = event.plain_result("这类敏感或高风险内容我不能协助处理。请换成合法、安全的技术问题。")
        if hasattr(event, "stop_event"):
            event.stop_event()
        setter = getattr(event, "set_result", None)
        if callable(setter):
            setter(result)
            return None
        return result

    @staticmethod
    def _normalize(text: str) -> str:
        text = text.lower().replace("？", "?").replace("：", ":")
        return re.sub(r"[\s,，。.!！?？:：;；、]", "", text)

    @staticmethod
    def _is_mentioned_in_group(event: AstrMessageEvent) -> bool:
        if not event.get_group_id():
            return True

        wake_checker = getattr(event, "is_at_or_wake_command", None)
        if callable(wake_checker):
            try:
                if wake_checker():
                    return True
            except TypeError:
                pass

        message_obj = getattr(event, "message_obj", None)
        raw = getattr(message_obj, "raw_message", None)
        self_id = getattr(message_obj, "self_id", None)
        if not self_id:
            getter = getattr(event, "get_self_id", None)
            if callable(getter):
                self_id = getter()

        message_getter = getattr(event, "get_messages", None)
        if callable(message_getter):
            for segment in message_getter() or []:
                if segment.__class__.__name__.lower() not in {"at", "mention"}:
                    continue
                target = (
                    getattr(segment, "qq", None)
                    or getattr(segment, "target", None)
                    or getattr(segment, "user_id", None)
                )
                if not self_id or str(target) == str(self_id):
                    return True

        raw_segments = raw if isinstance(raw, list) else []
        if isinstance(raw, dict):
            raw_segments = raw.get("message", [])
        for segment in raw_segments:
            if not isinstance(segment, dict):
                continue
            if str(segment.get("type", "")).lower() not in {"at", "mention"}:
                continue
            target = (segment.get("data") or {}).get("qq", "")
            if not self_id or str(target) == str(self_id):
                return True
        return False

    def _load_entries(self) -> None:
        try:
            mtime = self.faq_path.stat().st_mtime
            if mtime == self._mtime:
                return
            data = json.loads(self.faq_path.read_text(encoding="utf-8"))
            self._entries = [item for item in data if isinstance(item, dict)]
            self._semantic_vectors = []
            self._mtime = mtime
            logger.info("Passion FAQ loaded: %s entries", len(self._entries))
        except (OSError, ValueError, TypeError) as exc:
            logger.error("Failed to load Passion FAQ: %s", type(exc).__name__)

    def _find_answer(self, text: str) -> str | None:
        self._load_entries()
        normalized = self._normalize(text)
        best_answer: str | None = None
        best_score = 0
        for entry in self._entries:
            answer = str(entry.get("answer", "")).strip()
            if not answer:
                continue
            for trigger in entry.get("triggers", []):
                needle = self._normalize(str(trigger))
                score = 1000 + len(needle)
                if needle and needle in normalized and score > best_score:
                    best_score = score
                    best_answer = answer
            for group in entry.get("keyword_groups", []):
                needles = [
                    self._normalize(str(keyword))
                    for keyword in group
                    if str(keyword).strip()
                ]
                if not needles or not all(needle in normalized for needle in needles):
                    continue
                score = 500 + sum(len(needle) for needle in needles)
                if score > best_score:
                    best_score = score
                    best_answer = answer
        return best_answer

    @staticmethod
    def _embed_texts(texts: list[str]) -> list[list[float]]:
        request = urllib.request.Request(
            "http://sub2-ollama:11434/api/embed",
            data=json.dumps({"model": "bge-m3", "input": texts}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
        embeddings = payload.get("embeddings")
        if not isinstance(embeddings, list) or len(embeddings) != len(texts):
            raise ValueError("invalid embedding response")
        return embeddings

    async def _ensure_semantic_vectors(self) -> None:
        if self._semantic_vectors:
            return
        async with self._semantic_lock:
            if self._semantic_vectors:
                return
            samples: list[tuple[str, str, str]] = []
            for entry in self._entries:
                answer = str(entry.get("answer", "")).strip()
                entry_id = str(entry.get("id", ""))
                for sample in entry.get("semantic_examples", []):
                    text = str(sample).strip()
                    if answer and text:
                        samples.append((text, answer, entry_id))
            if not samples:
                return
            vectors = await asyncio.to_thread(
                self._embed_texts, [sample[0] for sample in samples]
            )
            self._semantic_vectors = [
                (vector, sample[1], sample[2])
                for vector, sample in zip(vectors, samples, strict=True)
            ]
            logger.info("Passion FAQ semantic index ready: %s samples", len(samples))

    async def _find_semantic_answer(self, text: str) -> str | None:
        try:
            await self._ensure_semantic_vectors()
            if not self._semantic_vectors:
                return None
            query_vector = (await asyncio.to_thread(self._embed_texts, [text]))[0]
        except Exception as exc:
            logger.warning("Passion FAQ semantic match unavailable: %s", type(exc).__name__)
            return None

        scores_by_id: dict[str, tuple[float, str]] = {}
        for vector, answer, entry_id in self._semantic_vectors:
            score = sum(a * b for a, b in zip(query_vector, vector, strict=True))
            if entry_id not in scores_by_id or score > scores_by_id[entry_id][0]:
                scores_by_id[entry_id] = (score, answer)
        ranked = sorted(scores_by_id.values(), key=lambda item: item[0], reverse=True)
        if not ranked:
            return None
        best_score, best_answer = ranked[0]
        second_score = ranked[1][0] if len(ranked) > 1 else 0.0
        if best_score < 0.72 or best_score - second_score < 0.035:
            return None
        return best_answer

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def answer_faq(self, event: AstrMessageEvent):
        if not self._is_mentioned_in_group(event):
            return
        text = str(getattr(event, "message_str", "") or "").strip()
        if not text and hasattr(event, "get_message_str"):
            text = str(event.get_message_str() or "").strip()
        if not text or text.startswith(("/", "／")):
            return
        if self._blocked_term(text):
            result = self._block_message(event)
            if result is not None:
                yield result
            return
        answer = self._find_answer(text)
        if not answer:
            answer = await self._find_semantic_answer(text)
        if not answer:
            return
        if hasattr(event, "stop_event"):
            event.stop_event()
        yield event.plain_result(answer)
