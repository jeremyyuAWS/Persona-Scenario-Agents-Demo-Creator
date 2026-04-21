"""FastAPI entry point for the Demo Designer Agent.

Run locally:
    uvicorn app.main:app --reload

With custom host/port:
    uvicorn app.main:app --host 0.0.0.0 --port 8000

In stub mode (no ANTHROPIC_API_KEY), the server returns deterministic blueprints
synthesized from keyword rules so the whole flow can be exercised offline.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app import __version__
from app.config import get_settings
from app.routes import approve, generate, retrieve, update
from app.services import tracing
from app.services.blueprint import BlueprintService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    # Initialize Langfuse (no-op if keys not set). Must come *before* any
    # service that might issue a traced call.
    tracing.init()
    logger.info(
        "startup",
        extra={
            "version": __version__,
            "stub_llm": settings.use_stub_llm,
            "tavily_enabled": settings.use_tavily,
            "tracing_enabled": tracing.is_enabled(),
            "model": settings.anthropic_model,
        },
    )
    # One BlueprintService per process. Stashed on app.state so routes can
    # pick it up via dependency injection.
    app.state.blueprint_service = BlueprintService()
    try:
        yield
    finally:
        # Flush buffered spans so short-lived processes don't drop traces.
        tracing.flush()


app = FastAPI(
    title="Demo Designer Agent",
    version=__version__,
    description=(
        "Converts free-form enterprise AI use cases into structured Demo "
        "Blueprints. Supports iterative refinement and produces an approved "
        "artifact for frontend rendering."
    ),
    lifespan=lifespan,
)

# Permissive CORS for local Open WebUI / Bolt integration in v1.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict:
    settings = get_settings()
    return {
        "status": "ok",
        "version": __version__,
        "stub_llm": settings.use_stub_llm,
        "tavily_enabled": settings.use_tavily,
        "tracing_enabled": tracing.is_enabled(),
    }


# Routers
app.include_router(generate.router, tags=["blueprint"])
app.include_router(update.router, tags=["blueprint"])
app.include_router(approve.router, tags=["blueprint"])
app.include_router(retrieve.router, tags=["blueprint"])


# Structured error envelope for uncaught validation / server errors
@app.exception_handler(ValidationError)
async def validation_error_handler(_: Request, exc: ValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"error": "validation_error", "detail": exc.errors()},
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(_: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled_error")
    return JSONResponse(
        status_code=500,
        content={"error": "internal_error", "detail": str(exc)},
    )
