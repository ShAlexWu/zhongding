"""FastAPI entrypoint: assembly only (no business logic)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1 import files, health, projects, rules, rulesets
from app.core.config import settings
from app.core.db import SessionLocal, check_db_writable, engine
from app.core.logging import get_logger, setup_logging
from app.models import Base
from app.schemas.common import ApiError
from app.services import task_service
from app.workers.worker import Runner

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    setup_logging()
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        recovered = task_service.recover_interrupted(session)
        synced = task_service.sync_rule_engines(session)
        session.commit()
        if recovered:
            logger.info("recovered %s interrupted project(s)", recovered)
        if synced:
            logger.info("synced rule metadata for %s existing rule result(s)", synced)
    finally:
        session.close()
    runner = Runner()
    runner.start()
    health.set_runner(runner)
    projects.set_runner(runner)
    rules.set_runner(runner)
    yield
    if runner._loop_task is not None:  # noqa: SLF001
        runner._loop_task.cancel()
        try:
            asyncio.get_event_loop().run_until_complete(runner._loop_task)  # noqa: SLF001
        except (asyncio.CancelledError, RuntimeError):
            pass


app = FastAPI(
    title="CIMC 智能审图 MVP API",
    version=settings.app_version,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
    max_age=86400,
)

API = settings.api_prefix


@app.exception_handler(ApiError)
async def api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http_status,
        content={"code": exc.code, "data": exc.data, "message": exc.message},
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled error")
    return JSONResponse(
        status_code=500,
        content={"code": 50000, "data": None, "message": "Internal server error"},
    )


app.include_router(health.router, prefix=API)
app.include_router(rulesets.router, prefix=API)
app.include_router(projects.router, prefix=API)
app.include_router(rules.router, prefix=API)
app.include_router(files.router, prefix=API)


@app.get("/")
def root() -> dict:
    return {"service": "zhongji-audit-backend", "status": "ok", "db_writable": check_db_writable()}
