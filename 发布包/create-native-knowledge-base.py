import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
import requests


BASE_URL = "http://127.0.0.1:6185/api/v1"
CONFIG_PATH = Path("/opt/passion-bot/data/cmd_config.json")
DOCUMENT_PATH = Path(
    sys.argv[1] if len(sys.argv) > 1 else "/tmp/Passion客户知识库-初版.md"
)
KB_NAME = "Passion 客户知识库"
PROVIDER_ID = "local-bge-m3"


def require_ok(response: requests.Response) -> dict:
    response.raise_for_status()
    payload = response.json()
    if payload.get("status") != "ok":
        raise RuntimeError(payload.get("message") or payload)
    return payload


def main() -> None:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    dashboard = config["dashboard"]
    token = jwt.encode(
        {
            "username": dashboard["username"],
            "exp": datetime.now(timezone.utc) + timedelta(minutes=10),
            "auth_source": "local-maintenance",
        },
        dashboard["jwt_secret"],
        algorithm="HS256",
    )
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {token}"

    listing = require_ok(
        session.get(f"{BASE_URL}/knowledge-bases?page=1&page_size=100", timeout=30)
    )["data"]
    items = listing.get("items", listing if isinstance(listing, list) else [])
    kb = next((item for item in items if item.get("kb_name") == KB_NAME), None)

    if kb is None:
        created = require_ok(
            session.post(
                f"{BASE_URL}/knowledge-bases",
                json={
                    "name": KB_NAME,
                    "description": "客户常见问题和标准答复，用于机器人优先检索。",
                    "emoji": "book",
                    "embedding_provider_id": PROVIDER_ID,
                    "chunk_size": 500,
                    "chunk_overlap": 50,
                    "top_k_dense": 5,
                    "top_k_sparse": 5,
                    "top_m_final": 5,
                },
                timeout=120,
            )
        )["data"]
        kb = created

    kb_id = kb.get("kb_id") or kb.get("id")
    documents = require_ok(
        session.get(
            f"{BASE_URL}/knowledge-bases/{kb_id}/documents?page=1&page_size=100",
            timeout=30,
        )
    )["data"]
    document_items = documents.get(
        "items", documents if isinstance(documents, list) else []
    )

    if not any(item.get("doc_name") == DOCUMENT_PATH.name for item in document_items):
        with DOCUMENT_PATH.open("rb") as document:
            uploaded = require_ok(
                session.post(
                    f"{BASE_URL}/knowledge-bases/{kb_id}/documents",
                    files={"file0": (DOCUMENT_PATH.name, document, "text/markdown")},
                    data={"chunk_size": "500", "chunk_overlap": "50"},
                    timeout=120,
                )
            )["data"]
        task_id = uploaded.get("task_id")
        if task_id:
            for _ in range(120):
                task = require_ok(
                    session.get(
                        f"{BASE_URL}/knowledge-bases/tasks/{task_id}", timeout=30
                    )
                )["data"]
                status = task.get("status")
                if status in {"completed", "success", "failed", "error"}:
                    if status in {"failed", "error"}:
                        raise RuntimeError(task)
                    break
                time.sleep(1)

    print(f"Native knowledge base ready: {KB_NAME} ({kb_id})")


if __name__ == "__main__":
    main()
