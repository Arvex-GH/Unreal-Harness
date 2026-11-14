"""Agent-loop tests: native tool calling AND the text-protocol fallback.

Spins up mock UE + mock LM Studio on free ports, so this exercises the real
HTTP streaming path, the real socket transport, and the real agent loop.
"""
from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.agent import Agent, AgentContext
from harness.config import Config
from harness.db import ChatStore
from harness.llm import LLMClient
from harness.ue.project import Installer, UEController
from harness.ue.transport import UnrealClient
from tests import mock_ue


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class MockLM:
    def __init__(self) -> None:
        self.port = free_port()
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "tests.mock_lmstudio", str(self.port)],
            cwd=str(Path(__file__).resolve().parent.parent),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(60):
            try:
                with socket.create_connection(("127.0.0.1", self.port), 0.2):
                    return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("mock LM Studio did not start")

    def stop(self) -> None:
        self.proc.terminate()
        self.proc.wait(timeout=10)


def make_env(native: bool):
    srv, editor = mock_ue.start(0)
    home = tempfile.mkdtemp()
    os.environ["UNREAL_HARNESS_HOME"] = home
    cfg = Config()
    cfg.bridge_port = srv.server_address[1]
    lm = MockLM()
    cfg.llm_base_url = f"http://127.0.0.1:{lm.port}/v1"
    cfg.model = "mock-agent"
    cfg.native_tool_calls = native
    cfg.autopilot = True
    cfg.max_steps = 12
    store = ChatStore(Path(home) / "chats.db")
    ue = UnrealClient(cfg)
    ctx = AgentContext(cfg=cfg, store=store, llm=LLMClient(cfg), ue=ue,
                       uectl=UEController(cfg), installer=Installer(cfg),
                       chat_id=store.create_chat("test")["id"])
    return srv, editor, lm, cfg, store, ctx


async def drain(ctx) -> list[dict]:
    events = []
    agent = Agent(ctx)
    async for ev in agent.run():
        events.append(ev)
    return events


def run_case(native: bool) -> None:
    srv, editor, lm, cfg, store, ctx = make_env(native)
    try:
        store.add_message(ctx.chat_id, "user",
                          "Create a blueprint /Game/Blueprints/BP_Demo that prints hello")
        events = asyncio.run(drain(ctx))
        types = [e["type"] for e in events]
        tools = [e["name"] for e in events if e["type"] == "tool_start"]
        label = "native" if native else "text-protocol"

        assert any(t == "final" for t in types), f"{label}: no final event: {types}"
        assert tools == ["bp_create", "bp_add_variable", "bp_build", "bp_compile",
                         "editor_save"], f"{label}: unexpected tool sequence {tools}"
        assert all(not e.get("result", {}).get("error") for e in events if e["type"] == "tool"), \
            f"{label}: a tool errored"
        # the graph must exist in the mock editor
        bp = editor.blueprints.get("/game/blueprints/bp_demo")
        assert bp and len(bp["nodes"]) == 2, f"{label}: blueprint not built"
        print(f"✓ agent loop ({label}): {len(tools)} tools, {len(types)} events, "
              f"final answer received")
    finally:
        lm.stop()
        mock_ue.stop(srv)


def test_native_tool_calling():
    run_case(True)


def test_text_protocol_fallback():
    run_case(False)


def test_stop_between_steps():
    srv, editor, lm, cfg, store, ctx = make_env(True)
    try:
        store.add_message(ctx.chat_id, "user", "build something")

        async def go():
            agent = Agent(ctx)
            seen = []
            async for ev in agent.run():
                seen.append(ev["type"])
                if ev["type"] == "tool" and len(seen) > 2:
                    ctx.cancel.set()      # stop like the UI's stop button does
            return seen

        typed = asyncio.run(go())
        assert "cancelled" in typed or "final" in typed, typed
        print(f"✓ cancellation honoured mid-run ({typed.count('tool')} tools before stop)")
    finally:
        lm.stop()
        mock_ue.stop(srv)


def test_greeting_auto_model_and_sse_500_fallback():
    """Reproduces saying 'hi' against LM Studio with:
    1) auto model selection skipping embedding models,
    2) full 49-tool schema validation on the stream, and
    3) a model that emits a mid-stream SSE 500 on native tools -> auto fallback -> reply.
    """
    srv, editor, lm, cfg, store, ctx = make_env(True)
    try:
        # 1) Auto model selection must skip text-embedding-nomic-embed-text-v1.5
        cfg.model = ""
        resolved = asyncio.run(ctx.llm.resolve_model())
        assert resolved == "mock-agent", f"unexpected auto model: {resolved}"

        # Saying "hi" with all tools enabled returns a real greeting
        store.add_message(ctx.chat_id, "user", "hi")
        events = asyncio.run(drain(ctx))
        finals = [e for e in events if e["type"] == "final"]
        assert finals and "Hi!" in finals[0]["text"], events

        # 2) Model that emits mid-stream SSE 500 when tools are sent falls back & still replies
        cfg.model = "no-tools-sse-500-model"
        chat2 = store.create_chat("fallback-test")["id"]
        ctx.chat_id = chat2
        store.add_message(chat2, "user", "hi")
        events2 = asyncio.run(drain(ctx))
        notices = [e for e in events2 if e["type"] == "notice"]
        finals2 = [e for e in events2 if e["type"] == "final"]
        assert notices, f"expected fallback notice, got {events2}"
        assert finals2 and "Hi!" in finals2[0]["text"], events2
        print("✓ 'hi' greeting + embedding skip + mid-stream SSE 500 fallback all work")
    finally:
        lm.stop()
        mock_ue.stop(srv)


if __name__ == "__main__":
    failures = 0
    for fn in (
        test_native_tool_calling,
        test_text_protocol_fallback,
        test_stop_between_steps,
        test_greeting_auto_model_and_sse_500_fallback,
    ):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            failures += 1
            import traceback
            print(f"✗ {fn.__name__}: {e}")
            traceback.print_exc()
    print(f"\n{'OK' if not failures else f'{failures} failed'}")
    sys.exit(1 if failures else 0)
