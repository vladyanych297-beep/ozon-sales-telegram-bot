from __future__ import annotations

import hashlib
import logging
from datetime import date
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse

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

    return app


app = create_app()
