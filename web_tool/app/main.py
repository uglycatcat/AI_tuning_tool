from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from web_tool.app.config import static_dir
from web_tool.app.routers import health, llm_debug, serial_debug

app = FastAPI(title="web_tool", version="0.1.0")

_static = static_dir()
if _static.is_dir():
    app.mount("/static", StaticFiles(directory=str(_static)), name="static")

app.include_router(health.router, prefix="/api")
app.include_router(serial_debug.router, prefix="/api")
app.include_router(llm_debug.router, prefix="/api")


@app.get("/")
def index() -> FileResponse:
    path = _static / "index.html"
    return FileResponse(path, media_type="text/html; charset=utf-8")
