import json
import os
import secrets
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
DATA = Path(os.getenv("FAQ_DATA_DIR", "/data"))
DB_PATH = DATA / "faq.db"
MEDIA = DATA / "media"
PASSWORD = os.getenv("FAQ_MANAGER_PASSWORD", "passion-faq-admin")
IMPORT_PATH = Path(os.getenv("FAQ_IMPORT_PATH", "/import/faq.json"))
DATA.mkdir(parents=True, exist_ok=True)
MEDIA.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Passion FAQ Manager")


def db() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_list(value: str) -> list:
    try:
        result = json.loads(value)
        return result if isinstance(result, list) else []
    except (TypeError, json.JSONDecodeError):
        return []


def row_payload(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"], "title": row["title"], "answer": row["answer"],
        "triggers": parse_list(row["triggers"]),
        "exact_triggers": parse_list(row["exact_triggers"]),
        "keyword_groups": parse_list(row["keyword_groups"]),
        "semantic_examples": parse_list(row["semantic_examples"]),
        "images": parse_list(row["images"]), "status": row["status"],
        "version": row["version"], "updated_at": row["updated_at"],
    }


def require_admin(x_faq_password: str = Header(default="")) -> None:
    if not secrets.compare_digest(x_faq_password, PASSWORD):
        raise HTTPException(401, "密码错误")


class EntryInput(BaseModel):
    id: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    title: str = Field(min_length=1, max_length=200)
    answer: str = Field(min_length=1, max_length=20000)
    triggers: list[str] = []
    exact_triggers: list[str] = []
    keyword_groups: list[list[str]] = []
    semantic_examples: list[str] = []
    images: list[dict] = []


def init() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    MEDIA.mkdir(parents=True, exist_ok=True)
    with db() as connection:
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS entries (
          id TEXT PRIMARY KEY, title TEXT NOT NULL, answer TEXT NOT NULL,
          triggers TEXT NOT NULL, exact_triggers TEXT NOT NULL,
          keyword_groups TEXT NOT NULL, semantic_examples TEXT NOT NULL,
          images TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft',
          version INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS revisions (
          revision_id INTEGER PRIMARY KEY AUTOINCREMENT, entry_id TEXT NOT NULL,
          version INTEGER NOT NULL, snapshot TEXT NOT NULL, created_at TEXT NOT NULL
        );
        """)
        count = connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        if count == 0 and IMPORT_PATH.exists():
            for item in json.loads(IMPORT_PATH.read_text(encoding="utf-8")):
                if not isinstance(item, dict) or not item.get("id") or not item.get("answer"):
                    continue
                connection.execute(
                    "INSERT INTO entries VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (str(item["id"]), str(item.get("title") or item["id"]), str(item["answer"]),
                     json.dumps(item.get("triggers", []), ensure_ascii=False),
                     json.dumps(item.get("exact_triggers", []), ensure_ascii=False),
                     json.dumps(item.get("keyword_groups", []), ensure_ascii=False),
                     json.dumps(item.get("semantic_examples", []), ensure_ascii=False),
                     "[]", "published", 1, now()),
                )


@app.on_event("startup")
def startup() -> None:
    init()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/public/entries")
def published_entries():
    with db() as connection:
        rows = connection.execute("SELECT * FROM entries WHERE status='published' ORDER BY title").fetchall()
    return {"version": max((row["version"] for row in rows), default=0), "entries": [row_payload(row) for row in rows]}


@app.get("/api/admin/entries", dependencies=[Depends(require_admin)])
def list_entries():
    with db() as connection:
        rows = connection.execute("SELECT * FROM entries ORDER BY updated_at DESC").fetchall()
    return [row_payload(row) for row in rows]


@app.put("/api/admin/entries/{entry_id}", dependencies=[Depends(require_admin)])
def save_entry(entry_id: str, item: EntryInput):
    if entry_id != item.id:
        raise HTTPException(400, "ID 不一致")
    with db() as connection:
        previous = connection.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
        version = int(previous["version"]) + 1 if previous else 1
        status = previous["status"] if previous else "draft"
        if previous:
            connection.execute("INSERT INTO revisions(entry_id,version,snapshot,created_at) VALUES(?,?,?,?)", (entry_id, previous["version"], json.dumps(row_payload(previous), ensure_ascii=False), now()))
        values = (item.title, item.answer, json.dumps(item.triggers, ensure_ascii=False), json.dumps(item.exact_triggers, ensure_ascii=False), json.dumps(item.keyword_groups, ensure_ascii=False), json.dumps(item.semantic_examples, ensure_ascii=False), json.dumps(item.images, ensure_ascii=False), status, version, now(), entry_id)
        connection.execute("INSERT INTO entries(id,title,answer,triggers,exact_triggers,keyword_groups,semantic_examples,images,status,version,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=?,answer=?,triggers=?,exact_triggers=?,keyword_groups=?,semantic_examples=?,images=?,status=?,version=?,updated_at=?", (entry_id, *values[:-1], *values[:-1]))
        row = connection.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
    return row_payload(row)


@app.post("/api/admin/entries/{entry_id}/publish", dependencies=[Depends(require_admin)])
def publish(entry_id: str):
    with db() as connection:
        changed = connection.execute("UPDATE entries SET status='published', updated_at=? WHERE id=?", (now(), entry_id)).rowcount
    if not changed:
        raise HTTPException(404, "知识不存在")
    return {"status": "published"}


@app.post("/api/admin/entries/{entry_id}/unpublish", dependencies=[Depends(require_admin)])
def unpublish(entry_id: str):
    with db() as connection:
        connection.execute("UPDATE entries SET status='draft', updated_at=? WHERE id=?", (now(), entry_id))
    return {"status": "draft"}


@app.get("/api/admin/entries/{entry_id}/revisions", dependencies=[Depends(require_admin)])
def revisions(entry_id: str):
    with db() as connection:
        rows = connection.execute("SELECT revision_id,version,created_at FROM revisions WHERE entry_id=? ORDER BY revision_id DESC", (entry_id,)).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/admin/entries/{entry_id}/rollback/{revision_id}", dependencies=[Depends(require_admin)])
def rollback(entry_id: str, revision_id: int):
    with db() as connection:
        revision = connection.execute("SELECT snapshot FROM revisions WHERE revision_id=? AND entry_id=?", (revision_id, entry_id)).fetchone()
        if not revision:
            raise HTTPException(404, "版本不存在")
        item = json.loads(revision["snapshot"])
        current = connection.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
        connection.execute("INSERT INTO revisions(entry_id,version,snapshot,created_at) VALUES(?,?,?,?)", (entry_id, current["version"], json.dumps(row_payload(current), ensure_ascii=False), now()))
        connection.execute("UPDATE entries SET title=?,answer=?,triggers=?,exact_triggers=?,keyword_groups=?,semantic_examples=?,images=?,version=?,updated_at=? WHERE id=?", (item["title"], item["answer"], json.dumps(item["triggers"], ensure_ascii=False), json.dumps(item["exact_triggers"], ensure_ascii=False), json.dumps(item["keyword_groups"], ensure_ascii=False), json.dumps(item["semantic_examples"], ensure_ascii=False), json.dumps(item["images"], ensure_ascii=False), current["version"] + 1, now(), entry_id))
    return {"status": "rolled_back"}


@app.post("/api/admin/media", dependencies=[Depends(require_admin)])
def upload_media(file: UploadFile = File(...)):
    suffix = Path(file.filename or "image").suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        raise HTTPException(400, "仅支持 PNG/JPG/WEBP/GIF")
    name = f"{uuid.uuid4().hex}{suffix}"
    with (MEDIA / name).open("wb") as target:
        shutil.copyfileobj(file.file, target)
    return {"url": f"/media/{name}", "name": file.filename or name}


app.mount("/media", StaticFiles(directory=MEDIA), name="media")
app.mount("/", StaticFiles(directory=ROOT / "static", html=True), name="static")
