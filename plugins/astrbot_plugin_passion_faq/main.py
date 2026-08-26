import asyncio
import json
import os
import re
import urllib.request
import urllib.parse
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
        self.mention_image_path = Path(__file__).with_name("assets") / "mention-empty.jpg"
        self.cache_path = Path("data/plugin_data/astrbot_plugin_passion_faq/entries-cache.json")
        self.media_dir = self.cache_path.parent / "media"
        self.service_url = os.getenv("FAQ_SERVICE_URL", "http://faq-manager:8000").rstrip("/")
        self._remote_checked_at = 0.0
        self._mtime = -1.0
        self._entries: list[dict[str, Any]] = []
        self._semantic_vectors: list[tuple[list[float], str, str]] = []
        self._semantic_lock = asyncio.Lock()
        self._blocked_terms = self._load_blocked_terms()
        self._load_entries()
        try:
            asyncio.get_running_loop().create_task(self._startup_sync())
        except RuntimeError:
            pass

    async def _startup_sync(self) -> None:
        await self._refresh_remote()
        await self._ensure_semantic_vectors()

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
        if re.search(
            r"(?:女仆|男仆|仆人|奴仆).{0,8}(?:装|服|服装|制服|扮演|角色扮演|换装)"
            r"|(?:装|服|服装|制服|扮演|角色扮演|换装).{0,8}(?:女仆|男仆|仆人|奴仆)",
            normalized,
        ):
            return "角色扮演服装"
        return None

    def _block_message(self, event: AstrMessageEvent):
        result = event.plain_result(
            "这类话题不在服务范围内。请咨询 API 接入、模型配置、计费或报错问题。"
        )
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
            source = self.cache_path if self.cache_path.exists() else self.faq_path
            mtime = source.stat().st_mtime
            if mtime == self._mtime:
                return
            data = json.loads(source.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data = data.get("entries", [])
            self._entries = [item for item in data if isinstance(item, dict)]
            self._semantic_vectors = []
            self._mtime = mtime
            logger.info("Passion FAQ loaded: %s entries", len(self._entries))
        except (OSError, ValueError, TypeError) as exc:
            logger.error("Failed to load Passion FAQ: %s", type(exc).__name__)

    def _refresh_remote_sync(self) -> None:
        request = urllib.request.Request(f"{self.service_url}/api/public/entries", headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=8) as response:
            payload = json.loads(response.read().decode("utf-8"))
        entries = payload.get("entries", [])
        if not isinstance(entries, list):
            raise ValueError("invalid FAQ snapshot")
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.cache_path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.cache_path)
        self._mtime = -1.0
        self._load_entries()

    async def _refresh_remote(self) -> None:
        loop = asyncio.get_running_loop()
        current = loop.time()
        if current - self._remote_checked_at < 30:
            return
        self._remote_checked_at = current
        try:
            await asyncio.to_thread(self._refresh_remote_sync)
        except Exception as exc:
            logger.warning("FAQ service unavailable; using local cache: %s", type(exc).__name__)

    def _find_answer(self, text: str) -> dict[str, Any] | None:
        self._load_entries()
        normalized = self._normalize(text)
        best_entry: dict[str, Any] | None = None
        best_score = 0
        for entry in self._entries:
            answer = str(entry.get("answer", "")).strip()
            if not answer:
                continue
            exact_triggers = {
                self._normalize(str(trigger))
                for trigger in entry.get("exact_triggers", [])
                if str(trigger).strip()
            }
            if normalized in exact_triggers:
                return entry
            for trigger in entry.get("triggers", []):
                needle = self._normalize(str(trigger))
                score = 1000 + len(needle)
                if needle and needle in normalized and score > best_score:
                    best_score = score
                    best_entry = entry
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
                    best_entry = entry
        # Keep the common Claude/anti-gravity 500 response deterministic even
        # when QQ mention formatting or a stale remote FAQ snapshot changes the
        # exact trigger text.
        if "500" in normalized and any(term in normalized for term in ("claude", "反重力", "可用账号耗尽", "availableaccountsexhausted")):
            return next((entry for entry in self._entries if str(entry.get("id", "")) == "accounts_exhausted"), best_entry)
        return best_entry

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

    async def _find_semantic_answer(self, text: str) -> dict[str, Any] | None:
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
        ranked = sorted(scores_by_id.items(), key=lambda item: item[1][0], reverse=True)
        if not ranked:
            return None
        best_id, (best_score, _) = ranked[0]
        second_score = ranked[1][1][0] if len(ranked) > 1 else 0.0
        if best_score < 0.72 or best_score - second_score < 0.035:
            return None
        return next((entry for entry in self._entries if str(entry.get("id", "")) == best_id), None)

    def _download_image_sync(self, url: str) -> Path:
        absolute = urllib.parse.urljoin(f"{self.service_url}/", url.lstrip("/"))
        suffix = Path(urllib.parse.urlparse(absolute).path).suffix or ".png"
        name = f"{abs(hash(absolute))}{suffix}"
        target = self.media_dir / name
        if not target.exists():
            self.media_dir.mkdir(parents=True, exist_ok=True)
            with urllib.request.urlopen(absolute, timeout=15) as response:
                target.write_bytes(response.read())
        return target

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def answer_faq(self, event: AstrMessageEvent):
        if not self._is_mentioned_in_group(event):
            return
        text = str(getattr(event, "message_str", "") or "").strip()
        if not text and hasattr(event, "get_message_str"):
            text = str(event.get_message_str() or "").strip()
        # QQ/NapCat may add non-breaking/invisible spaces or line breaks around a mention.
        mention_probe = re.sub(r"[\s\u200b-\u200f\u202a-\u202e\ufeff]+", "", text)
        mention_only = mention_probe.casefold() in {"@蛋黄", "＠蛋黄", "@3470541417", "＠3470541417"}
        # Only an explicit mention with no question should receive the helper image.
        # Empty/private image events can be echoes of our own outgoing media and must
        # not trigger another reply loop.
        if mention_only and self.mention_image_path.exists():
            if hasattr(event, "stop_event"):
                event.stop_event()
            yield event.image_result(str(self.mention_image_path.resolve()))
            return
        if not text or text.startswith(("/", "／")):
            return
        if self._blocked_term(text):
            result = self._block_message(event)
            if result is not None:
                yield result
            return
        await self._refresh_remote()
        entry = self._find_answer(text)
        if not entry:
            entry = await self._find_semantic_answer(text)
        if not entry:
            return
        if hasattr(event, "stop_event"):
            event.stop_event()
        yield event.plain_result(str(entry.get("answer", "")).strip())
        for image in entry.get("images", [])[:4]:
            url = str(image.get("url", "")).strip() if isinstance(image, dict) else ""
            if not url:
                continue
            try:
                image_path = await asyncio.to_thread(self._download_image_sync, url)
                yield event.image_result(str(image_path.resolve()))
            except Exception as exc:
                logger.warning("FAQ image unavailable (%s): %s", url, type(exc).__name__)
