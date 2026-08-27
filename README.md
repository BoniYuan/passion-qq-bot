# Sub2 QQ 机器人部署包

本目录提供一套可自行部署的 QQ 机器人：

- AstrBot：机器人主体、基础问答和知识库
- NapCatQQ：将普通 QQ 接入 OneBot 11
- `astrbot_plugin_sub2`：sub2 账号绑定、签到和额度查询
- 可选插件：NewAPI 模型健康度、机器人调用额度管理

## 你需要准备

1. 一台安装了 Docker 与 Docker Compose 的 Linux 服务器（推荐 Ubuntu 22.04/24.04，2 核 2 GB 起）。
2. 一个专门用于机器人的 QQ 小号。
3. sub2 中转站的地址，以及它的签到、用户信息 API。
4. 一个可供基础问答使用的 OpenAI 兼容 API Key。

> NapCatQQ 是非官方 QQ 接入方式，存在账号风控风险。不要使用重要 QQ 号。

## 一、启动 AstrBot

### Windows 小白安装

本机没有 Linux 服务器时，依次使用目录中的两个脚本：

1. 右键 `1-enable-wsl-RUN-AS-ADMIN.cmd`，选择“以管理员身份运行”，完成后重启电脑。
2. 重启后右键 `2-install-docker-RUN-AS-ADMIN.cmd`，选择“以管理员身份运行”。
3. 从开始菜单打开 Docker Desktop，接受协议，等待其显示运行正常。
4. 回到本目录，在地址栏输入 `powershell` 并回车，然后运行：

```powershell
powershell -ExecutionPolicy Bypass -File tools\init.ps1
docker compose up -d astrbot
```

浏览器访问 `http://localhost:6185`。电脑关机、睡眠或断网时，机器人会离线。

如果 Docker Desktop 提示 `WSL not installed`，先退出 Docker Desktop，右键 `3-fix-wsl-RUN-AS-ADMIN.cmd` 选择“以管理员身份运行”，成功后重启电脑。

### Linux 服务器安装

在服务器进入本目录：

```bash
chmod +x tools/init.sh
./tools/init.sh
docker compose up -d astrbot
```

在 Windows 上准备配置可运行 `powershell -ExecutionPolicy Bypass -File tools/init.ps1`。生成的 `.env` 和 `setup-secrets.txt` 都包含敏感信息，不要发送给别人。

浏览器访问 `http://服务器IP:6185`，完成 AstrBot 初始设置。然后在 AstrBot 中添加一个 OpenAI 兼容模型提供商，填写中转站的 Base URL、API Key 和模型名，即可用于基础问题解答。

## 二、安装 sub2 插件

本目录已把插件挂载进 AstrBot。首次启动后在 AstrBot WebUI 的插件页面重载插件；如果没有显示，重启容器：

```bash
docker compose restart astrbot
```

进入插件配置，至少填写：

- `base_url`：sub2 地址，例如 `https://api.example.com`
- `encryption_key`：运行 `python tools/generate_key.py` 生成
- `balance_endpoint`：查询用户/额度的接口路径
- `checkin_endpoint`：签到接口路径

插件默认按常见 NewAPI 风格读取数据，但不同面板的 API 不统一。配置项中的 `*_path` 使用点号读取嵌套 JSON，例如响应为：

```json
{"data":{"quota":12345,"used":2345}}
```

则设置 `balance_total_path=data.quota`、`balance_used_path=data.used`。

用户指令：

```text
/sub2帮助
/sub2绑定 sk-xxxx       # 只能私聊机器人
/sub2额度
/sub2签到
/sub2解绑               # 只能私聊机器人
```

## 三、接入 QQ

仓库已将 NapCat 完整运行镜像固定到经过验证的 SHA-256 digest。首次部署时 Docker
会下载同一版本的 NapCat、Linux QQ 和所需运行库，不需要把数百 MB 的第三方二进制
提交到 Git 仓库：

```powershell
docker compose --profile qq pull napcat
docker compose --profile qq up -d napcat
```

需要在无网络电脑部署时，可在有网络且已安装 Docker 的 Windows 电脑执行：

```powershell
powershell -ExecutionPolicy Bypass -File tools\export-napcat-image.ps1
```

将生成的 `napcat-image-amd64.tar` 单独带到目标电脑，再执行：

```powershell
powershell -ExecutionPolicy Bypass -File tools\import-napcat-image.ps1 -Archive .\napcat-image-amd64.tar
```

离线镜像包包含第三方软件且体积较大，不提交 GitHub；QQ 登录缓存同样保持本地私有。

先启动 NapCatQQ：

```bash
docker compose --profile qq up -d napcat
docker compose logs -f napcat
```

根据日志提示使用 QQ 小号扫码登录。打开 NapCat WebUI `http://服务器IP:6099`，创建一个 OneBot 11 **反向 WebSocket** 客户端。

AstrBot 和 NapCat 在同一个 Docker 网络内。反向 WebSocket 地址应填写 AstrBot WebUI 创建 QQ/OneBot 平台时显示的地址；主机名使用 `astrbot`，不要使用 `127.0.0.1`。Token 必须与 AstrBot 平台配置一致。

由于 AstrBot 不同版本生成的 OneBot 路径可能变化，应以当前 WebUI 显示值为准。

## 四、模型监控和额度管理

在 AstrBot 插件市场通过仓库地址安装：

```text
https://github.com/exynos967/astrbot_plugin_newapi_model_status
https://github.com/Dracowyn/astrbot_plugin_quota_hub
```

模型状态插件还依赖：

```text
https://github.com/james-6-23/new_api_tools
```

它适用于基于 `QuantumNous/new-api` 的中转站。若 sub2 不是 NewAPI，模型监控需要改成调用 sub2 自己的状态接口。

## 五、知识库问答

把价格说明、常见错误、充值和使用教程整理成 Markdown/PDF，在 AstrBot WebUI 中创建知识库并上传。系统提示词建议明确要求机器人只回答公开信息，不得输出 API Key、Cookie、后台地址或其他用户资料。

## 常用维护命令

```bash
docker compose ps
docker compose logs -f astrbot
docker compose restart astrbot
docker compose pull && docker compose up -d
```

数据保存在 `data/` 和 `napcat/`，升级容器不会丢失。部署前后都不要把 `.env`、`data/`、`napcat/` 提交到公开仓库。
