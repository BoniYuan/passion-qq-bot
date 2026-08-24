# 模型状态报告

独立的 Passion 模型监控报告服务，默认只监听本机 `http://127.0.0.1:6190`。

页面从 Passion 模型广场读取全部分组及模型，并关联最近 15 分钟的渠道监控数据。没有调用记录的模型仍会显示为“暂无数据”。本服务不保存 API Key、不主动探测模型，也不提供渠道或探测配置功能。

## 启动

在仓库根目录运行：

```powershell
docker compose up -d --build model-status-report
```

服务读取 `data/config/astrbot_plugin_passion_admin_config.json` 中的 Passion 地址和管理员凭据；凭据仅由后端使用，不会返回浏览器。

## 本地开发

```powershell
cd model-status-report
python -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt
$env:PASSION_ADMIN_CONFIG = "..\data\config\astrbot_plugin_passion_admin_config.json"
.venv\Scripts\uvicorn app.main:app --reload
npm install
npm run dev
```
