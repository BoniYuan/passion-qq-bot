import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .catalog import fetch_group_report

STATIC_DIR = Path(__file__).parent / "static"


def api_error(status: int, code: str, message: str, details=None) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message, "details": details})


app = FastAPI(title="模型状态报告", version="2.0.0")


@app.exception_handler(HTTPException)
async def handle_http_error(_: Request, exc: HTTPException):
    body = exc.detail if isinstance(exc.detail, dict) else {"code": "http_error", "message": str(exc.detail), "details": None}
    return JSONResponse(status_code=exc.status_code, content=body)


@app.exception_handler(RequestValidationError)
async def handle_validation_error(_: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"code": "validation_error", "message": "请求参数无效", "details": exc.errors()})


@app.get("/health")
def health():
    return {"status": "ok"}


async def build_report(group_id: int | None):
    try:
        return await fetch_group_report(group_id)
    except LookupError as exc:
        raise api_error(404, "group_not_found", str(exc)) from exc
    except RuntimeError as exc:
        raise api_error(502, "passion_api_failed", "Passion 监控数据获取失败", str(exc)) from exc


@app.get("/api/reports/summary")
async def report_summary(group_id: int | None = Query(default=None, ge=1)):
    return await build_report(group_id)


@app.get("/api/reports/export")
async def export_report(group_id: int | None = Query(default=None, ge=1)):
    content = json.dumps(await build_report(group_id), ensure_ascii=False, indent=2)
    return Response(content=content, media_type="application/json", headers={"Content-Disposition": "attachment; filename=model-status-report.json"})


@app.get("/api/reports/png")
async def export_report_png(group_id: int | None = Query(default=None, ge=1), search: str = Query(default="", max_length=200)):
    """Render the same report page used by the web UI as a PNG image."""
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise api_error(503, "renderer_unavailable", "PNG 渲染器不可用") from exc

    from urllib.parse import urlencode
    params = {}
    if group_id is not None:
        params["group_id"] = group_id
    if search.strip():
        params["search"] = search.strip()
    query = f"?{urlencode(params)}" if params else ""
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            executable_path="/usr/bin/chromium",
            args=["--no-sandbox", "--disable-crashpad", "--disable-crash-reporter", "--disable-breakpad"],
            env={"HOME": "/tmp", "XDG_CONFIG_HOME": "/tmp/chromium-config", "XDG_CACHE_HOME": "/tmp/chromium-cache"},
        )
        try:
            page = await browser.new_page(
                viewport={"width": 1600, "height": 900},
                device_scale_factor=1,
                timezone_id="Asia/Shanghai",
            )
            await page.goto(f"http://127.0.0.1:8000/{query}", wait_until="networkidle", timeout=60000)
            report = page.locator("[data-report-root]")
            await report.wait_for(state="visible", timeout=60000)
            await page.wait_for_function(
                "() => document.querySelector('[data-report-root]')?.dataset.reportReady === 'true'",
                timeout=60000,
            )
            await page.wait_for_function(
                "() => { const text = document.querySelector('[data-report-time-range]')?.textContent || ''; return text.includes(' 至 ') && !text.includes('--'); }",
                timeout=60000,
            )
            if search.strip():
                expected = search.strip().casefold()
                await page.wait_for_function(
                    "expected => (document.querySelector('[aria-label=\"搜索分组名称、模型名称或平台\"]')?.value || '').trim().toLocaleLowerCase() === expected",
                    arg=expected,
                    timeout=60000,
                )
            await page.wait_for_timeout(500)
            image = await report.screenshot(type="png", animations="disabled")
            return Response(content=image, media_type="image/png", headers={"Content-Disposition": "inline; filename=model-status-report.png"})
        finally:
            await browser.close()


@app.api_route(
    "/api/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    include_in_schema=False,
)
def unknown_api(path: str):
    raise api_error(404, "not_found", "API endpoint not found")


if STATIC_DIR.exists():
    assets = STATIC_DIR / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path == "api" or path.startswith("api/"):
            raise api_error(404, "not_found", "API endpoint not found")
        target = STATIC_DIR / path
        if path and target.is_file():
            return FileResponse(target)
        return FileResponse(STATIC_DIR / "index.html")
