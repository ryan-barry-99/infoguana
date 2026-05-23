import asyncio
import hmac
import logging
import threading
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app import backup, db, embed, onboard
from app.config import settings
from app.mcp_server import mcp
from app.routes import chat, notes, onboard as onboard_routes, plans as plan_routes, views


log = logging.getLogger(__name__)


class BearerAuthMiddleware:
    """ASGI middleware enforcing a shared-secret Bearer token."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        expected = f"Bearer {self.token}"
        for name, value in scope.get("headers", []):
            if name == b"authorization" and hmac.compare_digest(value.decode("latin-1"), expected):
                await self.app(scope, receive, send)
                return
        resp = JSONResponse({"error": "unauthorized"}, status_code=401,
                            headers={"WWW-Authenticate": "Bearer"})
        await resp(scope, receive, send)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    db.seed_protocol_if_missing(onboard.DEFAULT_PROTOCOL)
    n = db.reset_running_messages()
    if n:
        log.info("reset %d in-flight assistant message(s) to interrupted", n)
    chat.write_mcp_config()
    threading.Thread(target=embed.engine().warm, daemon=True).start()
    backup_task = asyncio.create_task(backup.scheduler())
    try:
        async with mcp.session_manager.run():
            yield
    finally:
        backup_task.cancel()
        try:
            await backup_task
        except (asyncio.CancelledError, Exception):
            pass


app = FastAPI(title="infoguana", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(views.router)
app.include_router(notes.router)
app.include_router(plan_routes.router)
app.include_router(chat.router)
app.include_router(onboard_routes.router)

# Mount the MCP Streamable HTTP app, wrapped in shared-secret auth.
mcp_app = mcp.streamable_http_app()
app.mount("/mcp", BearerAuthMiddleware(mcp_app, token=settings.mcp_secret))


def main() -> None:
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
