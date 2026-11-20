"""app.api.v1 – authenticated JSON REST adapter for the Daily Close PWA."""

from app.api.v1 import auth as _auth_module
from app.api.v1.endpoints import router

router.include_router(_auth_module.router)

__all__ = ["router"]
