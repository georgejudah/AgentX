"""Local web UI server for AgentX.

Binds to 127.0.0.1 only and requires a random per-session token on every
API and WebSocket call: the UI can submit tasks that drive the desktop, so
other pages open in the browser must not be able to reach it.
"""

import asyncio
import json
import logging
import secrets
import threading
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from gui_agents.ui.events import EventBus, bus as default_bus, read_history

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
COMMANDS = {"submit", "pause", "resume", "cancel"}


class Command(BaseModel):
    name: str
    text: str = ""


def create_app(bus: EventBus, token: str, port: int) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    allowed_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

    def check(t: str):
        if not secrets.compare_digest(t or "", token):
            raise HTTPException(status_code=401, detail="bad token")

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/state")
    def state(x_agentx_token: str = Header("")):
        check(x_agentx_token)
        return bus.snapshot()

    @app.get("/api/history")
    def history(x_agentx_token: str = Header("")):
        check(x_agentx_token)
        return read_history()

    @app.post("/api/command")
    def command(cmd: Command, x_agentx_token: str = Header("")):
        check(x_agentx_token)
        if cmd.name not in COMMANDS:
            raise HTTPException(status_code=400, detail="unknown command")
        if cmd.name == "submit" and not cmd.text.strip():
            raise HTTPException(status_code=400, detail="empty task")
        if not bus.command(cmd.name, {"text": cmd.text.strip()[:2000]}):
            raise HTTPException(status_code=503, detail="agent not attached")
        return {"ok": True}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        origin = websocket.headers.get("origin", "")
        if origin not in allowed_origins or not secrets.compare_digest(
            websocket.query_params.get("token", ""), token
        ):
            await websocket.close(code=4401)
            return
        await websocket.accept()
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()

        def listener(event):
            loop.call_soon_threadsafe(q.put_nowait, event)

        bus.subscribe(listener)
        try:
            await websocket.send_text(json.dumps({"type": "snapshot", "state": bus.snapshot()}))
            while True:
                event = await q.get()
                await websocket.send_text(json.dumps(event))
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            bus.unsubscribe(listener)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


def start_server(port: int = 8765, bus: EventBus = default_bus) -> str:
    """Start the UI server in a daemon thread and return the URL to open."""
    import uvicorn

    token = secrets.token_urlsafe(24)
    app = create_app(bus, token, port)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True, name="agentx-ui").start()
    return f"http://127.0.0.1:{port}/#token={token}"
