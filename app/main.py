"""
app/main.py

FastAPI application entrypoint for Daily Close.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.v1 import router as api_v1_router
from app.channels.web.routes import router as web_router
from app.kernel.db import get_session
from app.settings import settings


def create_app() -> FastAPI:
    app = FastAPI(
        title="Daily Close",
        description="Daily Close MVP for Birana Technology — Wholesale & Distribution Credit Management",
        version="0.5.0",
    )

    # Security headers middleware
    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "object-src 'none'; "
            "base-uri 'none'; "
            "form-action 'self'; "
            "frame-ancestors 'none';"
        )
        return response

    @app.get("/health")
    def health_check(session: Session = Depends(get_session)):
        """Local health check for monitoring and container orchestration."""
        try:
            session.execute(text("SELECT 1"))
            return {"status": "ok", "app": "daily-close", "database": "connected"}
        except Exception:
            return JSONResponse(
                status_code=503,
                content={"status": "unhealthy", "database": "disconnected"},
            )

    # CORS for the PWA. Added after the security-headers middleware so it is outermost and
    # CORS headers are present on error responses too. Origins are an explicit allowlist
    # (settings.cors_allowed_origins): a wildcard origin combined with credentials would let
    # any website make authenticated calls.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.mount("/static", StaticFiles(directory="app/channels/web/static"), name="static")
    app.include_router(web_router)
    app.include_router(api_v1_router)
    return app


app = create_app()
