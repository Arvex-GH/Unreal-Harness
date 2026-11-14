"""FastAPI app: serves the UI, chat streaming (SSE), settings, UE status, updater."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__, _read_version
from .agent import Agent, AgentContext, suggest_title
from .config import DATA_DIR, Config, ensure_dirs
from .db import ChatStore
from .llm import LLMClient
from .tools.base import registry
from .ue.project import Installer, UEController
from .ue.transport import UnrealClient
from . import updater

STATIC = Path(__file__).parent / "static"


class AppState:
    def __init__(self) -> None:
        ensure_dirs()
        self.cfg = Config.load()
        self.store = ChatStore()
        self.llm = LLMClient(self.cfg)
        self.ue = UnrealClient(self.cfg)
        self.uectl = UEController(self.cfg)
        self.installer = Installer(self.cfg)
        self.approvals: dict[str, asyncio.Future] = {}
        self.running: dict[str, bool] = {}
        try:
            self.installer.sync_existing_install()
        except Exception:
            pass

    def reload_config(self) -> None:
        self.cfg = Config.load()
        self.llm = LLMClient(self.cfg)
        self.ue = UnrealClient(self.cfg)
        self.uectl = UEController(self.cfg)
        self.installer = Installer(self.cfg)
        try:
            self.installer.sync_existing_install()
        except Exception:
            pass


state = AppState()


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_dirs()
    if state.cfg.check_updates_on_start:
        async def _bg():
            try:
                info = await updater.check(state.cfg)
                if info.get("update_available"):
                    print(f"[harness] update available: {info.get('latest')} "
                          f"(you have {info.get('current')})")
            except Exception:
                pass
        asyncio.create_task(_bg())
    yield
    state.ue.close()


app = FastAPI(title="Unreal Harness", version=__version__, lifespan=lifespan)

if STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


# --------------------------------------------------------------------- models
class ChatIn(BaseModel):
    title: str | None = None


class MessageIn(BaseModel):
    content: str
    autopilot: bool | None = None


class SettingsIn(BaseModel):
    values: dict[str, Any]


class ApprovalIn(BaseModel):
    request_id: str
    approved: bool


# ----------------------------------------------------------------------- pages
@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


@app.get("/api/version")
async def version():
    return {"version": _read_version(), "data_dir": str(DATA_DIR)}


# ----------------------------------------------------------------------- chats
@app.get("/api/chats")
async def list_chats():
    return {"chats": state.store.list_chats()}


@app.post("/api/chats")
async def create_chat(body: ChatIn):
    return state.store.create_chat(body.title or "New chat",
                                   str(state.cfg.project_file or ""))


@app.get("/api/chats/{chat_id}")
async def get_chat(chat_id: str):
    chat = state.store.get_chat(chat_id)
    if not chat:
        raise HTTPException(404, "chat not found")
    chat["messages"] = [{k: v for k, v in m.items() if not k.startswith("_")}
                        for m in state.store.messages(chat_id)]
    return chat


@app.delete("/api/chats/{chat_id}")
async def delete_chat(chat_id: str):
    state.store.delete_chat(chat_id)
    return {"deleted": chat_id}


@app.post("/api/chats/{chat_id}/rename")
async def rename_chat(chat_id: str, body: ChatIn):
    state.store.rename_chat(chat_id, body.title or "Untitled")
    return {"ok": True}


@app.get("/api/chats/{chat_id}/export")
async def export_chat(chat_id: str):
    return state.store.export_chat(chat_id)


# ------------------------------------------------------------------- chat turn
@app.post("/api/chats/{chat_id}/message")
async def post_message(chat_id: str, body: MessageIn):
    chat = state.store.get_chat(chat_id)
    if not chat:
        raise HTTPException(404, "chat not found")
    is_first = not state.store.messages(chat_id)
    state.store.add_message(chat_id, "user", body.content)
    if body.autopilot is not None:
        state.cfg.autopilot = body.autopilot

    # Set a quick provisional title right away without hitting LM Studio concurrently
    # while the main agent turn is about to stream; LLM title refinement runs after the turn.
    if is_first and (chat.get("title") or "New chat") == "New chat":
        quick = body.content.strip().replace("\n", " ")[:48]
        if quick and len(body.content.split()) <= 5:
            state.store.rename_chat(chat_id, quick)
    return {"ok": True, "first": is_first}


@app.post("/api/chats/{chat_id}/stop")
async def stop_turn(chat_id: str):
    state.running[chat_id] = False
    return {"stopped": True}


@app.get("/api/chats/{chat_id}/stream")
async def stream(chat_id: str, request: Request):
    """Server-Sent Events: runs the agent loop and streams everything it does."""
    if not state.store.get_chat(chat_id):
        raise HTTPException(404, "chat not found")

    queue: asyncio.Queue = asyncio.Queue()
    cancel = asyncio.Event()
    state.running[chat_id] = True

    async def emit(ev: dict) -> None:
        await queue.put(ev)

    async def approve(name: str, args: dict) -> bool:
        rid = f"{chat_id}-{time.time()}"
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        state.approvals[rid] = fut
        await queue.put({"type": "approval_request", "request_id": rid,
                         "tool": name, "args": args})
        try:
            return await asyncio.wait_for(fut, timeout=300)
        except asyncio.TimeoutError:
            return False
        finally:
            state.approvals.pop(rid, None)

    async def worker():
        ctx = AgentContext(cfg=state.cfg, store=state.store, llm=state.llm,
                           ue=state.ue, uectl=state.uectl, installer=state.installer,
                           chat_id=chat_id, approve=approve, emit=emit, cancel=cancel)
        agent = Agent(ctx)
        try:
            async for ev in agent.run():
                if not state.running.get(chat_id, True):
                    cancel.set()
                await queue.put(ev)
        except Exception as e:  # noqa: BLE001
            await queue.put({"type": "error", "error": f"{type(e).__name__}: {e}"})
        finally:
            try:
                chat_now = state.store.get_chat(chat_id)
                msgs_now = state.store.messages(chat_id)
                if (
                    chat_now
                    and (chat_now.get("title") or "New chat") == "New chat"
                    and msgs_now
                    and not cancel.is_set()
                ):
                    first_user = next(
                        (m["content"] for m in msgs_now if m.get("role") == "user"), ""
                    )
                    if first_user:
                        title = await suggest_title(state.llm, first_user)
                        if title:
                            state.store.rename_chat(chat_id, title)
            except Exception:
                pass
            await queue.put({"type": "__end__"})
            state.running[chat_id] = False

    task = asyncio.create_task(worker())

    async def gen():
        try:
            yield f"data: {json.dumps({'type': 'open'})}\n\n"
            while True:
                if await request.is_disconnected():
                    cancel.set()
                    task.cancel()
                    break
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if ev.get("type") == "__end__":
                    yield f"data: {json.dumps({'type': 'end'})}\n\n"
                    break
                yield f"data: {json.dumps(ev, default=str)}\n\n"
        finally:
            if not task.done():
                cancel.set()

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/approval")
async def approval(body: ApprovalIn):
    fut = state.approvals.get(body.request_id)
    if not fut:
        raise HTTPException(404, "no such approval request")
    if not fut.done():
        fut.set_result(body.approved)
    return {"ok": True}


# ------------------------------------------------------------------- settings
@app.get("/api/settings")
async def get_settings():
    return {"settings": state.cfg.public(), "tools": [
        {"name": t.name, "category": t.category, "risk": t.risk,
         "description": t.description.splitlines()[0]}
        for t in registry.all()]}


@app.post("/api/settings")
async def set_settings(body: SettingsIn):
    cfg = state.cfg
    allowed = {k for k in cfg.public().keys()}
    changed = {}
    for k, v in body.values.items():
        if k in allowed:
            setattr(cfg, k, v)
            changed[k] = v
    cfg.save()
    state.reload_config()
    return {"ok": True, "changed": list(changed.keys())}


@app.post("/api/settings/reset")
async def reset_settings():
    cfg = Config()
    cfg.save()
    state.reload_config()
    return {"ok": True}


# ------------------------------------------------------------------ llm health
@app.get("/api/llm/health")
async def llm_health():
    return await state.llm.health()


@app.get("/api/llm/models")
async def llm_models():
    try:
        return {"models": await state.llm.list_models()}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"models": [], "error": str(e)}, status_code=200)


# --------------------------------------------------------------------- unreal
@app.get("/api/ue/status")
async def ue_status():
    st = await asyncio.to_thread(state.ue.status)
    st["project_summary"] = await asyncio.to_thread(state.uectl.summary)
    st["plugin_installed"] = (
        state.installer.plugin_target() is not None
        and (state.installer.plugin_target() / "UEHarness.uplugin").exists())
    return st


@app.get("/api/ue/project")
async def ue_project():
    return await asyncio.to_thread(state.uectl.summary)


@app.post("/api/ue/install")
async def ue_install(body: dict | None = None):
    body = body or {}
    return await asyncio.to_thread(state.installer.install_plugin,
                                   build=body.get("build", True),
                                   enable_rc=body.get("enable_rc", True))


@app.post("/api/ue/launch")
async def ue_launch():
    from .tools.base import registry as reg

    class _Ctx:
        cfg = state.cfg
        ue = state.ue
        installer = state.installer
        uectl = state.uectl

    return await reg.execute("ue_launch_editor", {"wait": 1}, _Ctx())


@app.post("/api/ue/browse")
async def ue_browse(body: dict):
    """Pick a .uproject by path from the UI (no native dialog needed)."""
    path = body.get("path", "")
    from .config import discover_engine

    cfg = state.cfg
    cfg.project_path = path
    eng = discover_engine(path)
    if eng:
        cfg.engine_path = str(eng)
    cfg.save()
    state.reload_config()
    return {"ok": True, "settings": cfg.public()}


# --------------------------------------------------------------------- system
@app.get("/api/system/paths")
async def system_paths():
    return {
        "data_dir": str(DATA_DIR),
        "project_dir": str(state.cfg.project_dir or ""),
        "writable_roots": [str(p) for p in state.cfg.writable_roots],
        "config": str(DATA_DIR / "config.json"),
        "chats_db": str(DATA_DIR / "chats.db"),
        "log_dir": str(DATA_DIR / "logs"),
    }


# -------------------------------------------------------------------- updates
@app.get("/api/update/check")
async def update_check():
    return await updater.check(state.cfg, force=True)


@app.post("/api/update/apply")
async def update_apply():
    return await asyncio.to_thread(updater.apply, state.cfg)


@app.get("/api/update/history")
async def update_history():
    return {"history": updater.history()}


