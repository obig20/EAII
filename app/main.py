"""
app/main.py

FastAPI application entrypoint for Daily Close.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.channels.web.routes import router as web_router


def create_app() -> FastAPI:
    app = FastAPI(
        title="Daily Close",
        description="Daily Close MVP for Birana Technology — Wholesale & Distribution Credit Management",
        version="0.3.0",
    )
    app.include_router(web_router)
    return app


app = create_app()
