from __future__ import annotations

import asyncio
import uuid
import hashlib
import logging
from pathlib import Path
from datetime import date, datetime
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from ozon_sales_bot.ozon import OzonApiError

from .client import ForecastOzonClient
from .models import ForecastReport, WarehouseStockReport
from .service import ForecastService
from .settings import ServerSettings
from .version import APP_VERSION, VERSION_CODE
from .profitability import ProfitabilityService, CostInput


def create_app(service: ForecastService | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        client = None
        if service is None:
            settings = ServerSettings.from_env()
            client = ForecastOzonClient(settings.client_id, settings.api_key, settings.timezone)
            app.state.service = ForecastService(settings, client)
        else:
            app.state.service = service
        try:
            yield
        finally:
            tasks = [job["task"] for job in getattr(app.state, "profit_jobs", {}).values() if not job["task"].done()]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if client:
                await client.close()
    app = FastAPI(
        title="Ozon: план поставок",
        version=APP_VERSION,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.middleware("http")
    async def no_cache(request: Request, call_next):
        try:
            response = await call_next(request)
        except HTTPException as exc:
            response = JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        if request.url.path.startswith("/web/"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self'; connect-src 'self'; worker-src 'self'; "
                "manifest-src 'self'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
            )
            response.headers["Referrer-Policy"] = "same-origin"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        # Do not echo submitted parameters in validation responses.
        return JSONResponse(status_code=422, content={"detail": "Некорректные параметры запроса"})

    @app.exception_handler(OzonApiError)
    async def upstream_error(request: Request, exc: OzonApiError):
        return JSONResponse(
            status_code=503, content={"detail": str(exc)},
            headers={"Retry-After": str(getattr(exc, "retry_seconds", 30))}
        )

    @app.get("/health")
    async def health():
        return {"status": "ok", "service": "ozon-forecast", "schema_version": 1,
                "version": APP_VERSION}

    @app.get("/api/v1/forecast", response_model=ForecastReport)
    async def forecast(
        request: Request,
        days: int = Query(30, ge=1, le=365),
        sales_days: int = Query(30, ge=1, le=365),
    ):
        return await request.app.state.service.report(days, sales_days)

    @app.get("/api/v1/warehouse-stocks", response_model=WarehouseStockReport)
    async def warehouse_stocks(request: Request):
        return await request.app.state.service.warehouse_stocks()

    def profitability_service(request: Request):
        if not hasattr(request.app.state, "profitability"):
            source = request.app.state.service
            request.app.state.profitability = ProfitabilityService(source.settings, source.client)
        return request.app.state.profitability

    @app.get("/api/v1/profitability")
    async def profitability(request: Request, date_from: date, date_to: date, refresh: bool = False):
        try:
            report = await profitability_service(request).report(date_from, date_to, refresh)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if report is None:
            raise HTTPException(404, "Для этого периода ещё нет сохранённого отчёта")
        return report

    @app.post("/api/v1/profitability/jobs")
    async def start_profitability_job(request: Request, date_from: date, date_to: date, full_reload: bool = False):
        source = profitability_service(request)
        today = datetime.now(ZoneInfo(source.settings.timezone)).date()
        if date_from > date_to or date_to > today or (date_to - date_from).days >= 365:
            raise HTTPException(422, "Выберите период от 1 до 365 дней, не позднее сегодня")
        jobs = getattr(request.app.state, "profit_jobs", {})
        request.app.state.profit_jobs = jobs
        for job_id, job in jobs.items():
            if job["status"] == "running":
                if job["period"] == (date_from, date_to) and job.get("full_reload", False) == full_reload:
                    return {"job_id": job_id}
                raise HTTPException(409, "Другой отчёт уже загружается. Дождитесь завершения.")
        # Keep only the most recent completed jobs. Financial reports remain in SQLite.
        while len(jobs) >= 10:
            del jobs[next(iter(jobs))]
        job_id = uuid.uuid4().hex
        job = {"status": "running", "period": (date_from, date_to), "full_reload": full_reload}
        jobs[job_id] = job
        async def run():
            try:
                job["report"] = await source.report(date_from, date_to, True, full_reload=True) if full_reload else await source.report(date_from, date_to, True)
                job["status"] = "complete"
            except (OzonApiError, ValueError) as exc:
                job.update(status="error", error=str(exc))
            except Exception:
                logging.exception("Background profitability report failed")
                job.update(status="error", error="Не удалось сформировать отчёт. Повторите обновление.")
        job["task"] = asyncio.create_task(run())
        return {"job_id": job_id}

    @app.get("/api/v1/profitability/jobs/{job_id}")
    async def profitability_job(request: Request, job_id: str):
        job = getattr(request.app.state, "profit_jobs", {}).get(job_id)
        if job is None:
            raise HTTPException(404, "Загрузка не найдена. Повторите обновление.")
        return {k: v for k, v in job.items() if k in ("status", "report", "error")} | {
            "progress": getattr(profitability_service(request), "progress", "Получаем заказы и начисления…")}

    @app.post("/api/v1/profitability/cost")
    async def save_cost(request: Request, value: CostInput):
        profitability_service(request).store.save(value)
        return value.model_dump()

    def published_apk(request: Request):
        path = request.app.state.service.settings.update_apk_path
        if not path.is_file() or path.stat().st_size == 0:
            raise HTTPException(404, "Обновление пока не опубликовано")
        return path

    @app.get("/api/v1/update")
    async def update_info(request: Request):
        path = published_apk(request)
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return {"version_code": VERSION_CODE, "version_name": APP_VERSION,
                "size_bytes": path.stat().st_size, "sha256": digest.hexdigest()}

    @app.get("/api/v1/update/apk")
    async def update_apk(request: Request):
        path = published_apk(request)
        return FileResponse(path, media_type="application/vnd.android.package-archive",
                            filename=f"ozon-plan-{APP_VERSION}.apk")

    # Keep the web shell in a narrow mount: API, APK and Android routes retain
    # their existing contracts. StaticFiles never exposes the project directory.
    # Older update packages may not contain web assets. Never prevent the API
    # from starting when only its Python files have been transferred.
    web_path = Path(__file__).with_name("web")
    if web_path.is_dir():
        @app.get("/", include_in_schema=False)
        async def web_home():
            return RedirectResponse("/web/", status_code=307)

        app.mount("/web", StaticFiles(directory=web_path, html=True), name="web")

    return app


app = create_app()
