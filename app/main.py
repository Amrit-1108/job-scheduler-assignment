import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.routes import auth, health, jobs
from app.core.config import get_settings
from app.core.db import init_db
from app.core.logging import setup_logging
from app.domain.errors import DomainError

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    setup_logging()

    init_db()
    if get_settings().jwt_secret == "dev-secret-change-me":
        log.warning(
            "JWT_SECRET is still the built-in dev value - fine locally, "
            "never deploy like this"
        )
    log.info("API ready")
    yield


app = FastAPI(
    title="Job Scheduler & Execution Engine",
    version="1.0.0",
    description=(
        "Schedule one-off, interval and cron jobs, and let a pool of workers "
        "execute them exactly once with retries and crash recovery.\n\n"
        "Every `/jobs` endpoint needs a bearer token: register at "
        "`/auth/register`, then hit **Authorize** at the top right.\n\n"
        "Interactive docs: `/docs` - OpenAPI schema: `/openapi.json`"
    ),
    lifespan=lifespan,
)


@app.exception_handler(DomainError)
async def domain_error_handler(_: Request, exc: DomainError):
    """Domain errors carry their own status code, so the routes stay clean."""
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": exc.code, "message": exc.message},
    )


app.include_router(health.router)
app.include_router(auth.router)
app.include_router(jobs.router)


@app.get("/", include_in_schema=False)
def root():
    return {"service": "job-scheduler", "docs": "/docs"}
