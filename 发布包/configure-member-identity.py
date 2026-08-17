import json
from pathlib import Path


config_path = Path("/opt/passion-bot/data/cmd_config.json")
with config_path.open("r", encoding="utf-8-sig") as handle:
    config = json.load(handle)

platform_settings = config.setdefault("platform_settings", {})
platform_settings["unique_session"] = True
platform_settings["reply_with_mention"] = True
platform_settings["reply_with_quote"] = True

provider_settings = config.setdefault("provider_settings", {})
provider_settings["identifier"] = False
provider_settings["group_name_display"] = False

with config_path.open("w", encoding="utf-8") as handle:
    json.dump(config, handle, ensure_ascii=False, indent=2)
    handle.write("\n")

print("Per-member sessions are enabled without exposing IDs or group names.")
