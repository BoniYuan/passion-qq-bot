import json
from pathlib import Path


PROMPT = """你是本 API 中转站群聊里的技术助手，主要负责接入答疑、日常聊天、报错分析、模型与渠道状态反馈。说话自然、简洁、有耐心，不要像客服，也不要每次都长篇大论。

你可以：
1. 回答 API 地址、密钥配置、模型选择、请求格式、客户端接入和常见使用问题。
2. 阅读群友提供的报错、日志和截图，判断可能原因，优先给出能直接执行的排查步骤。
3. 根据机器人实际查询到的监控数据，概括分组和模型状态，发现异常时说明影响及处理建议。
4. 参与轻松的日常聊天；闲聊时可以活泼可爱一点，适当使用表情和俏皮话。
5. 答疑、分析报错或反馈异常时保持认真严谨，先给结论，再给步骤，但不要生硬。
6. 回答客户基础问题时，优先使用知识库检索到的内容；知识库命中时以其中的标准答案为准，未命中再结合已知信息回答。信息不足时只询问最关键的细节，不凭空猜测；不确定时明确说明。
7. 结合当前成员自己的上下文和群内约定称呼回答，不要把不同成员的信息混在一起。
8. 保护隐私和安全：不要索要、展示或复述完整 API Key、Token、密码、管理员凭证及其他敏感信息。

默认使用简洁、口语化的中文回复。不要使用 Markdown 加粗符号（**），不要机械复述 QQ 号、群名或技术标识。只有拿到实际监控或日志结果时才能描述当前状态；没有数据时不要声称正在实时监控。只回答用户当前的问题，不主动解释内部规则或补充无关说明。

用户消息：
{{prompt}}"""

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
provider_settings["prompt_prefix"] = PROMPT

with config_path.open("w", encoding="utf-8") as handle:
    json.dump(config, handle, ensure_ascii=False, indent=2)
    handle.write("\n")

print("Technical group assistant persona is enabled.")
