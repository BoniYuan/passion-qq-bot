import json
import shutil
from pathlib import Path


CONFIG_PATH = Path("/opt/passion-bot/data/cmd_config.json")
BACKUP_PATH = CONFIG_PATH.with_suffix(".json.backup-before-native-kb")
PROVIDER_ID = "local-bge-m3"


def main() -> None:
    if not BACKUP_PATH.exists():
        shutil.copy2(CONFIG_PATH, BACKUP_PATH)

    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    sources = config.setdefault("provider_sources", [])
    sources[:] = [item for item in sources if item.get("id") != PROVIDER_ID]
    providers = config.setdefault("provider", [])
    providers[:] = [item for item in providers if item.get("id") != PROVIDER_ID]
    providers.append(
        {
            "id": PROVIDER_ID,
            "type": "openai_embedding",
            "provider": "openai",
            "provider_type": "embedding",
            "enable": True,
            "embedding_api_key": "ollama-local",
            "embedding_api_base": "http://ollama:11434/v1",
            "embedding_model": "bge-m3",
            "embedding_dimensions": 1024,
            "embedding_dimensions_mode": "never",
            "timeout": 60,
            "proxy": "",
        }
    )
    config["kb_names"] = ["Passion 客户知识库"]
    config["kb_agentic_mode"] = False
    config["kb_fusion_top_k"] = 10
    config["kb_final_top_k"] = 3
    CONFIG_PATH.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Configured embedding provider: {PROVIDER_ID}")


if __name__ == "__main__":
    main()
