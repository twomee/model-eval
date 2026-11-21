"""FastAPI app factory: wiring only (middleware, routers, static files).

Local-only threat model: the server runs on your machine with your CLI logins, so a
request to it can spend your subscription quota. "It's only localhost" isn't enough,
because any website you open can make your browser send requests to localhost:
  * Host allow-list: blocks DNS rebinding (evil.com re-resolving to 127.0.0.1 to READ responses).
  * POST bodies must be application/json: a cross-origin page can only send that after a
    CORS preflight, and we enable no CORS, so the browser blocks it. Plain HTML forms can't send JSON.
  * The server binds 127.0.0.1 by default (see `serve`), so it isn't reachable from your network.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ..config import ConfigError
from .routes import models, plan
from .services.models_service import ModelsService
from .services.plan_service import PlanService

WEB_DIST = Path(__file__).parent.parent / "web"  # `npm run build` in web/ writes here
LOCAL_HOSTS = ["127.0.0.1", "localhost", "[::1]"]


def create_app(config_path: Path, tasks_dir: Path, allowed_hosts: list[str] | None = None) -> FastAPI:
    app = FastAPI(title="evalkit", version="0.3.0", docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.models_service = ModelsService(config_path=config_path, tasks_dir=tasks_dir)
    app.state.plan_service = PlanService(config_path=config_path, tasks_dir=tasks_dir)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts or LOCAL_HOSTS)

    @app.middleware("http")
    async def require_json_for_writes(request: Request, call_next):
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
            if ctype != "application/json":
                return JSONResponse({"detail": "Content-Type must be application/json"}, status_code=415)
        return await call_next(request)

    @app.exception_handler(ConfigError)
    async def config_error(_request: Request, exc: ConfigError) -> JSONResponse:
        # Config is re-read per request, so a typo in config.yaml surfaces here. Say what's wrong
        # (the UI shows `detail`) instead of a bare 500 and a blank page.
        return JSONResponse({"detail": f"Invalid configuration: {exc}"}, status_code=500)

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(models.router)
    app.include_router(plan.router)
    _mount_frontend(app)
    return app


def _mount_frontend(app: FastAPI) -> None:
    index = WEB_DIST / "index.html"
    if not index.exists():
        @app.get("/", include_in_schema=False)
        def not_built() -> JSONResponse:
            return JSONResponse({"detail": "UI not built: run `npm ci && npm run build` in web/. "
                                           "The API is available at /api/docs."}, status_code=503)
        return
    app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> FileResponse:
        # Client-side routing: any non-API path serves the SPA shell. Unknown API paths
        # must stay a JSON 404, or a typo in the client "succeeds" with an HTML page.
        if path == "api" or path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not Found")
        # Root files from the build (favicon.svg). resolve() + is_relative_to() is the
        # path-traversal guard: "../../config.yaml" must not escape the build directory.
        root = WEB_DIST.resolve()
        candidate = (root / path).resolve()
        if path and candidate.is_relative_to(root) and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(index)
