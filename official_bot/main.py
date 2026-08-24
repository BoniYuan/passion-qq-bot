import os
import asyncio
import re
import json
import time
import uuid
import sqlite3
from datetime import datetime, timedelta, timezone
import aiohttp
from pathlib import Path
import botpy
from botpy.message import GroupMessage, C2CMessage

APP_ID = os.getenv("QQ_OFFICIAL_APP_ID", "").strip()
APP_SECRET = os.getenv("QQ_OFFICIAL_APP_SECRET", "").strip()
if not APP_ID or not APP_SECRET:
    raise RuntimeError(
        "QQ_OFFICIAL_APP_ID and QQ_OFFICIAL_APP_SECRET are required "
        "when the official profile is enabled"
    )
TEST_GROUP = os.getenv("QQ_OFFICIAL_TEST_GROUP", "")
SUPER_ADMIN_IDS = {x.strip() for x in os.getenv("QQ_OFFICIAL_ADMIN_IDS", "").split(",") if x.strip()}
LIVE_OPERATIONS = os.getenv("QQ_OFFICIAL_LIVE_OPERATIONS", "false").lower() == "true"
PENDING = {}
CLAIM_DB = Path("/app/data/claims.db")
CLAIM_DB.parent.mkdir(parents=True, exist_ok=True)
with sqlite3.connect(CLAIM_DB) as db:
    db.execute("CREATE TABLE IF NOT EXISTS test_claims (user_openid TEXT PRIMARY KEY, email TEXT NOT NULL, operation_id TEXT NOT NULL, claimed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
    db.execute("CREATE TABLE IF NOT EXISTS bot_admins (user_openid TEXT PRIMARY KEY, added_by TEXT NOT NULL, added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
    db.execute("CREATE TABLE IF NOT EXISTS bot_admin_audit (id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL, target_openid TEXT NOT NULL, operator_openid TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
    db.execute("CREATE TABLE IF NOT EXISTS bot_admin_requests (code TEXT PRIMARY KEY, user_openid TEXT NOT NULL, group_openid TEXT NOT NULL, expires_at REAL NOT NULL)")
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

def claim_email(text: str) -> str | None:
    candidate = text.strip().lower()
    return candidate if EMAIL_RE.fullmatch(candidate) else None

# qq-botpy 1.2.1 only retains member_openid, while some group mention events
# identify the mentioned member with id, user_openid, or openid.
def _group_user_init(self, data):
    self.member_openid = data.get("member_openid") or data.get("user_openid") or data.get("openid") or data.get("id")

GroupMessage._User.__init__ = _group_user_init

def is_super_admin(user_openid: str) -> bool:
    return bool(user_openid) and user_openid in SUPER_ADMIN_IDS

def is_regular_admin(user_openid: str) -> bool:
    if not user_openid:
        return False
    with sqlite3.connect(CLAIM_DB) as db:
        return db.execute("SELECT 1 FROM bot_admins WHERE user_openid = ?", (user_openid,)).fetchone() is not None

def is_any_admin(user_openid: str) -> bool:
    return is_super_admin(user_openid) or is_regular_admin(user_openid)

def add_regular_admin(user_openid: str, operator_openid: str) -> bool:
    if not user_openid or is_super_admin(user_openid):
        return False
    with sqlite3.connect(CLAIM_DB) as db:
        cursor = db.execute(
            "INSERT OR IGNORE INTO bot_admins(user_openid, added_by) VALUES (?, ?)",
            (user_openid, operator_openid),
        )
        if cursor.rowcount:
            db.execute(
                "INSERT INTO bot_admin_audit(action, target_openid, operator_openid) VALUES ('add', ?, ?)",
                (user_openid, operator_openid),
            )
        return bool(cursor.rowcount)

def remove_regular_admin(user_openid: str, operator_openid: str) -> bool:
    with sqlite3.connect(CLAIM_DB) as db:
        cursor = db.execute("DELETE FROM bot_admins WHERE user_openid = ?", (user_openid,))
        if cursor.rowcount:
            db.execute(
                "INSERT INTO bot_admin_audit(action, target_openid, operator_openid) VALUES ('remove', ?, ?)",
                (user_openid, operator_openid),
            )
        return bool(cursor.rowcount)

def list_regular_admins() -> list[tuple[str, str]]:
    with sqlite3.connect(CLAIM_DB) as db:
        return db.execute("SELECT user_openid, added_at FROM bot_admins ORDER BY added_at").fetchall()

def create_admin_request(user_openid: str, group_openid: str) -> str:
    with sqlite3.connect(CLAIM_DB) as db:
        db.execute("DELETE FROM bot_admin_requests WHERE expires_at < ? OR user_openid = ?", (time.time(), user_openid))
        while True:
            code = f"{uuid.uuid4().int % 1000000:06d}"
            try:
                db.execute(
                    "INSERT INTO bot_admin_requests(code, user_openid, group_openid, expires_at) VALUES (?, ?, ?, ?)",
                    (code, user_openid, group_openid, time.time() + 600),
                )
                return code
            except sqlite3.IntegrityError:
                continue

def consume_admin_request(code: str, group_openid: str) -> str:
    with sqlite3.connect(CLAIM_DB) as db:
        db.execute("DELETE FROM bot_admin_requests WHERE expires_at < ?", (time.time(),))
        row = db.execute(
            "SELECT user_openid FROM bot_admin_requests WHERE code = ? AND group_openid = ?",
            (code, group_openid),
        ).fetchone()
        if row:
            db.execute("DELETE FROM bot_admin_requests WHERE code = ?", (code,))
        return str(row[0]) if row else ""

def mentioned_member_openid(message) -> str:
    sender = str(getattr(message.author, "member_openid", ""))
    candidates = [
        str(getattr(user, "member_openid", ""))
        for user in getattr(message, "mentions", [])
        if getattr(user, "member_openid", None) and str(getattr(user, "member_openid")) != sender
    ]
    return candidates[-1] if candidates else ""

def masked_openid(user_openid: str) -> str:
    return user_openid if len(user_openid) <= 10 else f"{user_openid[:4]}...{user_openid[-4:]}"

def admin_help(is_super: bool) -> str:
    if not is_super:
        return "普通管理员指令：\n\n• 单独发送 邮箱：领取 $15 测试额度"
    return (
        "超级管理员指令：\n\n"
        "• 单独发送 邮箱：领取 $15 测试额度\n"
        "• /充值 邮箱 金额：进入充值预览\n"
        "• /退款 邮箱 金额：进入退款预览\n"
        "• 确认 / 取消：处理充值或退款预览\n"
        "• /兑换码 金额 [数量]：生成兑换码\n"
        "• /报错 邮箱：查询最近五条报错\n"
        "• /添加管理员 申请码：添加普通管理员\n"
        "• /删除管理员 @成员：删除普通管理员\n"
        "• /管理员列表：查看普通管理员\n\n"
        "添加管理员：对方先发送 /申请管理员，你再使用返回的申请码。"
    )

def parse_balance_command(text: str, max_amount: float):
    parts = text.split()
    if len(parts) < 3:
        raise ValueError("用法：/充值 <邮箱> <金额>，例如 /充值 user@example.com 15")
    action = parts[0].lstrip("/")
    email = parts[1].strip().lower()
    if action not in {"充值", "退款"}:
        raise ValueError("不支持的操作类型。")
    if not EMAIL_RE.fullmatch(email):
        raise ValueError("邮箱格式不正确。")
    if not re.fullmatch(r"\d+(?:\.\d{1,2})?", parts[2]):
        raise ValueError("金额格式错误，应填写 15 或 15.00。")
    amount = float(parts[2])
    if amount < 0.01 or amount > max_amount:
        raise ValueError(f"金额必须在 0.01 到 {max_amount:.2f} 之间。")
    return action, email, amount

def load_admin_config():
    try:
        return json.loads(Path("/app/admin-config.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}

async def exact_user(email: str):
    cfg = load_admin_config()
    base = str(cfg.get("base_url", "")).rstrip("/")
    if not base or not cfg.get("admin_email") or not cfg.get("admin_password"):
        return None, "管理员 API 配置不完整。"
    timeout = aiohttp.ClientTimeout(total=int(cfg.get("request_timeout", 15)))
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(base + "/api/v1/auth/login", json={"email": cfg["admin_email"], "password": cfg["admin_password"]}) as resp:
            login = await resp.json(content_type=None)
        token = login.get("data", {}).get("access_token") or login.get("access_token")
        if not token:
            return None, "管理员 API 登录失败。"
        async with session.get(base + "/api/v1/admin/users", params={"page": 1, "page_size": 200, "search": email}, headers={"Authorization": "Bearer " + str(token)}) as resp:
            payload = await resp.json(content_type=None)
    data = payload.get("data", payload) if isinstance(payload, dict) else {}
    items = data.get("items", []) if isinstance(data, dict) else []
    matches = [x for x in items if isinstance(x, dict) and str(x.get("email", "")).lower() == email.lower()]
    if len(matches) != 1:
        return None, f"邮箱精确匹配数量为 {len(matches)}，已停止操作。"
    user = matches[0]
    return {"email": user.get("email", email), "id": user.get("id"), "balance": user.get("balance")}, None

async def apply_balance(user_id: str, amount: float, action: str, notes: str):
    cfg = load_admin_config()
    base = str(cfg.get("base_url", "")).rstrip("/")
    timeout = aiohttp.ClientTimeout(total=int(cfg.get("request_timeout", 15)))
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(base + "/api/v1/auth/login", json={"email": cfg["admin_email"], "password": cfg["admin_password"]}) as resp:
            login = await resp.json(content_type=None)
        token = login.get("data", {}).get("access_token") or login.get("access_token")
        if not token:
            raise ValueError("管理员 API 登录失败")
        operation = "add" if action == "充值" else "subtract"
        async with session.post(base + f"/api/v1/admin/users/{user_id}/balance", json={"balance": amount, "operation": operation, "notes": notes}, headers={"Authorization": "Bearer " + str(token)}) as resp:
            payload = await resp.json(content_type=None)
            if resp.status < 200 or resp.status >= 300:
                raise ValueError("资金接口返回失败")
    data = payload.get("data", payload) if isinstance(payload, dict) else {}
    return data.get("balance") if isinstance(data, dict) else None

async def generate_redeem_codes(amount: float, count: int):
    cfg = load_admin_config()
    base = str(cfg.get("base_url", "")).rstrip("/")
    timeout = aiohttp.ClientTimeout(total=int(cfg.get("request_timeout", 15)))
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(base + "/api/v1/auth/login", json={"email": cfg["admin_email"], "password": cfg["admin_password"]}) as resp:
            login = await resp.json(content_type=None)
        token = login.get("data", {}).get("access_token") or login.get("access_token")
        if not token:
            raise ValueError("管理员 API 登录失败")
        body = {"count": count, "type": "balance", "value": amount}
        async with session.post(base + "/api/v1/admin/redeem-codes/generate", json=body, headers={"Authorization": "Bearer " + str(token)}) as resp:
            payload = await resp.json(content_type=None)
            if not 200 <= resp.status < 300:
                raise ValueError("兑换码接口返回失败")
    data = payload.get("data", payload) if isinstance(payload, dict) else []
    codes = [str(x.get("code", "")).strip() for x in data if isinstance(x, dict) and x.get("code")]
    if len(codes) != count:
        raise ValueError("兑换码返回数量不正确")
    return codes

async def has_admin_test_credit(user_id: str) -> bool:
    cfg = load_admin_config()
    base = str(cfg.get("base_url", "")).rstrip("/")
    timeout = aiohttp.ClientTimeout(total=int(cfg.get("request_timeout", 15)))
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(base + "/api/v1/auth/login", json={"email": cfg["admin_email"], "password": cfg["admin_password"]}) as resp:
            login = await resp.json(content_type=None)
        token = login.get("data", {}).get("access_token") or login.get("access_token")
        if not token:
            raise ValueError("管理员 API 登录失败")
        page = 1
        while True:
            params = {"page": page, "page_size": 200, "type": "admin_balance"}
            async with session.get(base + f"/api/v1/admin/users/{user_id}/balance-history", params=params, headers={"Authorization": "Bearer " + str(token)}) as resp:
                payload = await resp.json(content_type=None)
            data = payload.get("data", payload) if isinstance(payload, dict) else {}
            items = data.get("items", []) if isinstance(data, dict) else []
            for item in items:
                try:
                    if abs(float(item.get("value", 0)) - 15.0) < 0.0001:
                        return True
                except (TypeError, ValueError):
                    continue
            pages = int(data.get("pages", 1)) if isinstance(data, dict) else 1
            if page >= pages or not items:
                return False
            page += 1

async def recent_user_errors(email: str):
    user, error = await exact_user(email)
    if error:
        raise ValueError(error)
    cfg = load_admin_config()
    base = str(cfg.get("base_url", "")).rstrip("/")
    timeout = aiohttp.ClientTimeout(total=int(cfg.get("request_timeout", 15)))
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(base + "/api/v1/auth/login", json={"email": cfg["admin_email"], "password": cfg["admin_password"]}) as resp:
            login = await resp.json(content_type=None)
        token = login.get("data", {}).get("access_token") or login.get("access_token")
        if not token:
            raise ValueError("管理员 API 登录失败")
        results = []
        target_id = str(user["id"])
        china_tz = timezone(timedelta(hours=8))
        now = datetime.now(china_tz)
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_time = (today - timedelta(days=1)).isoformat()
        end_time = (today + timedelta(days=1) - timedelta(microseconds=1)).isoformat()
        page = 1
        while page <= 50:
            params = {"page": page, "page_size": 20, "view": "all", "user_id": user["id"], "start_time": start_time, "end_time": end_time, "sort_by": "created_at", "sort_order": "desc"}
            async with session.get(base + "/api/v1/admin/ops/errors", params=params, headers={"Authorization": "Bearer " + str(token)}) as resp:
                payload = await resp.json(content_type=None)
                if not 200 <= resp.status < 300:
                    raise ValueError("报错查询接口返回失败")
            data = payload.get("data", payload) if isinstance(payload, dict) else {}
            items = data.get("items", []) if isinstance(data, dict) else []
            for item in items:
                same_id = str(item.get("user_id", "")) == target_id
                same_email = str(item.get("user_email", "")).lower() == email.lower()
                if same_id or same_email:
                    results.append(item)
                    if len(results) == 5:
                        return results
            if not items or page >= int(data.get("pages", 1)):
                break
            page += 1
    return results

def format_recent_errors(email: str, items: list[dict]) -> str:
    if not items:
        return f"{email} 暂无后台报错记录。"
    owner_labels = {
        "platform": "平台错误",
        "user": "用户错误",
        "upstream": "上游错误",
    }
    lines = [f"{email} 最近 {len(items)} 条报错："]
    for index, item in enumerate(items, 1):
        raw_message = item.get("message") or item.get("error_message") or item.get("upstream_error_message") or "未知错误"
        message = re.sub(r"\s+", " ", str(raw_message)).strip()[:180]
        model = str(item.get("requested_model") or item.get("model") or "未知模型")[:60]
        status = str(item.get("status_code") or "-")
        owner = str(item.get("error_owner") or "").lower()
        category = owner_labels.get(owner, str(item.get("type") or owner or "未知分类"))
        severity = str(item.get("severity") or "-")
        created_at = str(item.get("created_at") or "-")[:19].replace("T", " ")
        lines.append(
            f"{index}. {created_at}\n"
            f"分类：{category} | 状态码：{status} | 级别：{severity}\n"
            f"模型：{model}\n响应内容：{message}"
        )
    return "\n".join(lines)

def claim_exists(user_openid: str) -> bool:
    with sqlite3.connect(CLAIM_DB) as db:
        return db.execute("SELECT 1 FROM test_claims WHERE user_openid = ?", (user_openid,)).fetchone() is not None

def save_claim(user_openid: str, email: str, operation_id: str) -> None:
    with sqlite3.connect(CLAIM_DB) as db:
        db.execute("INSERT INTO test_claims(user_openid, email, operation_id) VALUES (?, ?, ?)", (user_openid, email, operation_id))
FAQ_PATH = Path(__file__).with_name("faq.json")
SENSITIVE_PATH = Path(__file__).with_name("sensitive_words.json")

def blocked_topic(text: str) -> bool:
    normalized = re.sub(r"\s+", "", text).lower()
    try:
        terms = json.loads(SENSITIVE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        terms = []
    if any(str(term).strip().lower() in normalized for term in terms if str(term).strip()):
        return True
    if re.search(r"(?:叫|喊)(?:我|你)?(?:爸爸|妈妈|爷爷|奶奶|哥哥|姐姐|弟弟|妹妹|主人|老公|老婆|宝贝)", normalized):
        return True
    return bool(re.search(r"(?:女仆|男仆|仆人|奴仆).{0,8}(?:装|服|服装|制服|扮演|角色扮演|换装)|(?:装|服|服装|制服|扮演|角色扮演|换装).{0,8}(?:女仆|男仆|仆人|奴仆)", normalized))

def faq_answer(text: str) -> str | None:
    try:
        entries = json.loads(FAQ_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    normalized = re.sub(r"[\s,，。.!！?？:：;；、]", "", text.lower())
    best = None
    score = 0
    for entry in entries:
        answer = str(entry.get("answer", "")).strip()
        for trigger in entry.get("triggers", []):
            needle = re.sub(r"[\s,，。.!！?？:：;；、]", "", str(trigger).lower())
            if needle and needle in normalized and 1000 + len(needle) > score:
                best, score = answer, 1000 + len(needle)
        for group in entry.get("keyword_groups", []):
            needles = [re.sub(r"\s+", "", str(x).lower()) for x in group]
            if needles and all(x in normalized for x in needles) and 500 + sum(map(len, needles)) > score:
                best, score = answer, 500 + sum(map(len, needles))
    return best

class PassionOfficialBot(botpy.Client):
    async def on_ready(self):
        print("official QQ bot ready", flush=True)

    async def on_group_at_message_create(self, message: GroupMessage):
        text = re.sub(r"^\s*", "", (message.content or "").strip())
        sender = str(getattr(message.author, "member_openid", ""))
        if text in {"测试", "test", "ping"}:
            await message.reply(content="官方机器人已上线，回复链路正常。")
        elif text in {"帮助", "菜单", "机器人功能", "/机器人功能", "充值帮助", "/充值帮助"}:
            await message.reply(content=(
                "官方机器人功能：\n"
                "1. FAQ/API 接入答疑\n"
                "2. 邮箱识别与隐私提醒\n"
                "3. 私聊绑定后查询额度：/sub2绑定、/sub2额度、/sub2签到\n"
                "4. 管理员充值、退款、兑换码功能正在迁移，需私聊并验证权限。\n"
                "请勿在群里发送密码、API Key 或访问令牌。"
            ))
        elif text in {"sub2帮助", "/sub2帮助", "额度", "/sub2额度"}:
            await message.reply(content="额度查询需先私聊机器人执行 /sub2绑定 <访问令牌>，绑定后使用 /sub2额度。令牌只会加密保存。")
        elif text in {"/管理员帮助", "管理员帮助"}:
            if not is_any_admin(sender):
                await message.reply(content="无权操作。管理员帮助仅限管理员查看。")
                return
            await message.reply(content=admin_help(is_super_admin(sender)))
        elif text in {"/申请管理员", "申请管理员"}:
            if is_any_admin(sender):
                await message.reply(content="你已经是管理员，无需重复申请。")
                return
            code = create_admin_request(sender, str(getattr(message, "group_openid", "")))
            await message.reply(content=f"管理员申请已创建，10 分钟内有效。\n请超级管理员发送：/添加管理员 {code}")
        elif text.startswith(("/添加管理员", "/删除管理员")):
            if not is_super_admin(sender):
                await message.reply(content="无权操作。管理员管理仅限超级管理员。")
                return
            target = mentioned_member_openid(message)
            if not target and text.startswith("/添加管理员"):
                code_match = re.fullmatch(r"/添加管理员\s+(\d{6})", text)
                if code_match:
                    target = consume_admin_request(code_match.group(1), str(getattr(message, "group_openid", "")))
            if not target:
                await message.reply(content="未识别到有效申请。请让对方先发送 /申请管理员，再发送 /添加管理员 <申请码>。")
                return
            if text.startswith("/添加管理员"):
                if is_super_admin(target):
                    await message.reply(content="该成员已经是超级管理员。")
                elif add_regular_admin(target, sender):
                    await message.reply(content=f"普通管理员添加成功：{masked_openid(target)}\n权限：仅测试额度")
                else:
                    await message.reply(content="该成员已经是普通管理员。")
            elif remove_regular_admin(target, sender):
                await message.reply(content=f"普通管理员已删除：{masked_openid(target)}")
            else:
                await message.reply(content="该成员不是普通管理员，或不能被删除。")
        elif text in {"/管理员列表", "管理员列表"}:
            if not is_super_admin(sender):
                await message.reply(content="无权操作。管理员列表仅限超级管理员查看。")
                return
            admins = list_regular_admins()
            details = "\n".join(f"{index}. {masked_openid(openid)}（{added_at}）" for index, (openid, added_at) in enumerate(admins, 1))
            await message.reply(content=f"普通管理员：{len(admins)} 人" + (f"\n{details}" if details else ""))
        elif text.startswith(("/充值", "/退款", "/兑换码", "/确认操作")):
            await message.reply(content="管理员操作暂只接受私聊，并要求配置管理员权限；请不要在群里发送邮箱、金额或确认码。")
        elif text.startswith("/报错"):
            if not is_super_admin(sender):
                await message.reply(content="无权操作。报错查询仅限超级管理员使用。")
                return
            match = re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", text, re.I)
            if not match:
                await message.reply(content="用法：/报错 <用户邮箱>")
                return
            try:
                email = match.group(0).lower()
                await message.reply(content=format_recent_errors(email, await recent_user_errors(email)))
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError):
                await message.reply(content="报错查询失败，请稍后重试或检查邮箱。")
        elif blocked_topic(text):
            await message.reply(content="这类话题不在服务范围内。请咨询 API 接入、模型配置、计费或报错问题。")
        elif re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", text, re.I):
            email = re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", text, re.I).group(0).lower()
            if not is_any_admin(sender):
                await message.reply(content="无权操作。测试额度领取仅限管理员账号执行。")
                return
            user, error = await exact_user(email)
            if error:
                await message.reply(content=error)
            else:
                try:
                    if await has_admin_test_credit(str(user["id"])):
                        await message.reply(content="该邮箱已有管理员发放的 $15 测试额度，不能重复领取。")
                        return
                    balance = await apply_balance(user["id"], 15.0, "充值", "测试额度-15刀")
                    suffix = f"最新余额：${float(balance):.2f}" if balance is not None else ""
                    await message.reply(content=f"测试额度领取成功：$15.00\n邮箱：{email}\n{suffix}")
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError):
                    await message.reply(content="测试额度发放失败，请稍后重试。")
        else:
            answer = faq_answer(text)
            if answer:
                await message.reply(content=answer)

    async def on_c2c_message_create(self, message: C2CMessage):
        text = (message.content or "").strip()
        sender = str(getattr(message.author, "user_openid", ""))
        if not is_any_admin(sender):
            return
        if text in {"/管理员帮助", "管理员帮助"}:
            await message.reply(content=admin_help(is_super_admin(sender)))
            return
        if not is_super_admin(sender):
            allowed = claim_email(text) is not None
            if not allowed:
                if text.startswith(("/报错", "/充值", "/退款", "/兑换码", "/确认操作", "/添加管理员", "/删除管理员", "/管理员列表")) or text in {"确认", "确定", "取消"}:
                    await message.reply(content="无权操作。普通管理员仅可领取测试额度。")
                return
        if blocked_topic(text) and not text.startswith(("/充值", "/退款", "/兑换码", "/确认操作")):
            await message.reply(content="这类话题不在服务范围内。请咨询 API 接入、模型配置、计费或报错问题。")
            return
        if text.startswith("/报错"):
            match = re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", text, re.I)
            if not match:
                await message.reply(content="用法：/报错 <用户邮箱>")
                return
            try:
                email = match.group(0).lower()
                await message.reply(content=format_recent_errors(email, await recent_user_errors(email)))
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError):
                await message.reply(content="报错查询失败，请稍后重试或检查邮箱。")
            return
        email = claim_email(text)
        if email:
            try:
                user, error = await exact_user(email)
                if error:
                    await message.reply(content=error)
                    return
                if await has_admin_test_credit(str(user["id"])):
                    await message.reply(content="该邮箱已有管理员发放的 $15 测试额度，不能重复领取。")
                    return
                operation_id = uuid.uuid4().hex[:12]
                balance = await apply_balance(user["id"], 15.0, "充值", "测试额度-15刀")
                suffix = f"最新余额：${float(balance):.2f}" if balance is not None else ""
                await message.reply(content=f"测试额度领取成功：$15.00\n邮箱：{email}\n{suffix}")
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError):
                await message.reply(content="测试额度发放失败，请稍后重试。")
            return
        if text in {"帮助", "机器人功能", "/机器人功能"}:
            await message.reply(content="官方机器人已启用 FAQ、邮箱隐私提醒和额度查询指引。管理员充值/退款功能正在接入安全审批流程。")
        elif text.startswith(("/充值", "/退款", "/兑换码", "/确认操作")):
            if not is_super_admin(sender):
                await message.reply(content="无权操作。充值、退款和兑换码仅限超级管理员。")
            elif text in {"确认", "确定"} or text.startswith("/确认操作"):
                item = PENDING.get(str(message.author.user_openid))
                if not item or item["expires"] < time.time():
                    await message.reply(content="没有待确认操作，或确认码已过期。")
                else:
                    PENDING.pop(str(message.author.user_openid), None)
                    await message.reply(content="确认已记录。实际资金接口尚未启用，本次未执行充值或退款。")
            else:
                cfg = load_admin_config()
                if text.startswith("/兑换码"):
                    parts = text.split()
                    try:
                        amount = float(parts[1])
                        count = int(parts[2]) if len(parts) > 2 else 1
                        if amount < 0.01 or amount > float(cfg.get("max_amount", 1000)):
                            raise ValueError
                        if count < 1 or count > int(cfg.get("max_code_count", 20)):
                            raise ValueError
                        codes = await generate_redeem_codes(amount, count)
                        await message.reply(content=f"兑换码生成成功\n单张额度：${amount:.2f}\n数量：{count}\n" + "\n".join(codes))
                    except (IndexError, ValueError, aiohttp.ClientError, asyncio.TimeoutError):
                        await message.reply(content="用法：/兑换码 <金额> [数量]，金额和数量必须在管理员配置范围内。")
                    return
                try:
                    action, email, amount = parse_balance_command(text, float(cfg.get("max_amount", 1000)))
                except (TypeError, ValueError) as exc:
                    await message.reply(content=str(exc))
                    return
                if action in {"充值", "退款"}:
                    try:
                        user, error = await exact_user(email)
                    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                        user, error = None, "用户查询接口暂时不可用。"
                    if error:
                        await message.reply(content=error)
                        return
                    if action == "退款":
                        try:
                            if float(amount) > float(user.get("balance") or 0):
                                await message.reply(content=f"退款金额不能超过用户当前余额（${float(user.get('balance') or 0):.2f}）。")
                                return
                        except (TypeError, ValueError):
                            await message.reply(content="用户余额数据异常，已停止操作。")
                            return
                operation_id = uuid.uuid4().hex[:12]
                PENDING[str(message.author.user_openid)] = {
                    "id": operation_id, "action": action, "email": email,
                    "amount": f"{amount:.2f}", "user_id": user.get("id"),
                    "balance": user.get("balance"), "expires": time.time() + 120,
                }
                await message.reply(content=(
                    f"操作预览（编号 {operation_id}）\n"
                    f"类型：{action}\n邮箱：{email}\n金额：${amount:.2f}\n"
                    f"当前余额：${float(user.get('balance') or 0):.2f}\n用户：已精确匹配唯一账号\n"
                    "请核对后回复“确认”或“取消”。确认后将正式执行资金变更。"
                ))
        elif text in {"确认", "确定", "取消"}:
            if is_super_admin(sender):
                if text == "取消":
                    PENDING.pop(str(message.author.user_openid), None)
                    await message.reply(content="已取消待确认操作。")
                else:
                    item = PENDING.get(str(message.author.user_openid))
                    if not item or item["expires"] < time.time():
                        PENDING.pop(str(message.author.user_openid), None)
                        await message.reply(content="没有待确认操作，或确认码已过期。")
                    else:
                        PENDING.pop(str(message.author.user_openid), None)
                        if not LIVE_OPERATIONS:
                            await message.reply(content=f"已确认操作 {item['id']}。当前为迁移保护模式，未执行资金变更。")
                        else:
                            try:
                                new_balance = await apply_balance(item["user_id"], float(item["amount"]), item["action"], "官方机器人管理员操作")
                                await message.reply(content=f"操作 {item['id']} 已执行成功。最新余额：${float(new_balance):.2f}" if new_balance is not None else f"操作 {item['id']} 已执行成功。")
                            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError):
                                await message.reply(content=f"操作 {item['id']} 执行失败，未确认资金变更结果，请检查后台记录。")
        elif text in {"/sub2额度", "额度"}:
            await message.reply(content="请先私聊发送 /sub2绑定 <访问令牌>，绑定后再查询额度。不要把令牌发到群里。")

if __name__ == "__main__":
    intents = botpy.Intents(public_messages=True)
    bot = PassionOfficialBot(intents=intents)
    bot.run(appid=APP_ID, secret=APP_SECRET)
