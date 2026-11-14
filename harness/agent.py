"""The agent loop: plan -> act -> observe, with an autopilot that keeps going
until the task is actually finished.

Key behaviours that make this feel like a real engineer rather than a chatbot:
  * executes multiple tool calls per turn (native parallel tool calling)
  * keeps working after errors (reads the error, fixes, retries)
  * context pruning so long UE sessions don't blow the local model's window
  * streaming events to the UI: text deltas, tool calls, tool results
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from .config import Config
from .db import ChatStore
from .llm import LLMClient, LLMError, parse_text_tool_calls
from .tools.base import registry

SYSTEM_PROMPT = """You are Unreal Harness, an AI game developer with full control of a real Unreal Engine 5 editor and its project files.

You are not a documentation bot. You DO things: you create blueprints, wire nodes, spawn actors, edit C++ files, run builds, read compiler errors, and fix them.

# How you work
1. Inspect before you act. Use ue_status, ue_search_assets, bp_read_graph, fs_read, fs_grep to learn the real state. Never invent asset paths, class names, variable names or pin names.
2. Prefer the fastest path: `bp_build` builds and wires an entire blueprint graph in ONE call and compiles it. Use `bp_add_node`/`bp_connect` only for surgical single changes.
3. Verify. After blueprint work call bp_compile; after C++ call ue_build and read `errors`; after level work call level_find_actors.
4. When something fails, read the error text and correct yourself. Do not repeat an identical failing call. If a tool is unavailable, fall back to ue_python or the fs_* tools.
5. Keep going until the goal is met. Only stop when the task is done, or when you genuinely need the user to decide something.

# Conventions
- Blueprint paths look like /Game/Blueprints/BP_Door. Never include the .uasset extension.
- Function names are real UFunction names. Use ue_find_function when unsure (PrintString, K2_GetActorLocation, SetActorLocation, ...).
- Event names: ReceiveBeginPlay, ReceiveTick, ReceiveActorBeginOverlap (the C++ names), not "BeginPlay".
- Pin spec format is `nodeGuid:PinName`; `then`/`execute` resolve to the first exec pin.
- After creating or changing assets, call editor_save so the work isn't lost.
- Report what you changed in plain language, briefly. No filler.

# Safety
- Destructive operations (delete, overwrite, force-push) are allowed only when the user's goal needs them. Prefer copying over overwriting.
"""

TEXT_PROTOCOL_ADDENDUM = """
# Tool protocol (IMPORTANT)
You cannot use native function calling in this session. To call a tool, emit a fenced block exactly like this:

```tool_call
{"tool": "bp_build", "args": {"bp": "/Game/BP_Door", "nodes": [ ... ], "links": [ ... ]}}
```

You may emit several blocks in one reply; they run in order. After each tool result you get a new turn with an observation. Never invent tool output. Write normal prose for the user alongside the blocks.
"""


@dataclass
class AgentContext:
    cfg: Config
    store: ChatStore
    llm: LLMClient
    ue: Any
    uectl: Any
    installer: Any
    chat_id: str
    approve: Any = None          # async callable(tool_name, args) -> bool
    step: int = 0
    emit: Any = None             # async callable(event: dict)
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    transcript: list[dict] = field(default_factory=list)
    _pending: list[dict] = field(default_factory=list)


def _approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def prune(messages: list[dict], budget: int) -> list[dict]:
    """Keep system + recent history; shrink old tool outputs to stubs."""
    total = sum(_approx_tokens(m.get("content") or "") for m in messages)
    if total <= budget:
        return messages
    out = list(messages)
    # 1) stub out old tool results
    for i, m in enumerate(out):
        if i < len(out) - 16 and m.get("role") == "tool":
            if len(m.get("content") or "") > 400:
                out[i] = {**m, "content": (m["content"][:300] + " ...[trimmed]")}
    # 2) drop oldest non-system messages until under budget
    while total > budget and len(out) > 8:
        for i, m in enumerate(out):
            if m.get("role") != "system":
                removed = out.pop(i)
                total -= _approx_tokens(removed.get("content") or "")
                break
        else:
            break
    return out


class Agent:
    def __init__(self, ctx: AgentContext):
        self.ctx = ctx

    # ------------------------------------------------------------- history
    def _history(self) -> list[dict]:
        msgs = self.ctx.store.messages(self.ctx.chat_id)
        clean = []
        for m in msgs:
            m = {k: v for k, v in m.items() if not k.startswith("_")}
            if m["role"] == "tool" and not m.get("tool_call_id"):
                continue
            # an assistant message with tool_calls must be followed by tool results;
            # if the turn was interrupted mid-way, drop the orphaned call record
            if m["role"] == "assistant" and m.get("tool_calls") and not m["content"]:
                m = {**m, "content": ""}
            clean.append(m)
        return clean

    def _system_messages(self) -> list[dict]:
        ue = self.ctx.ue
        st = ue.status()
        cfg = self.ctx.cfg
        tools_note = "" if cfg.native_tool_calls else TEXT_PROTOCOL_ADDENDUM

        project = cfg.project_file
        tool_count = len(registry.schemas(cfg))
        env = f"""# Environment
- Transport: {st['transport']} ({'editor connected' if st['editor_connected'] else 'EDITOR NOT CONNECTED - use file tools or ue_launch_editor'})
- Project: {project or 'not configured'}
- Engine: {cfg.engine_path or 'not configured'}
- Tools available: {tool_count}
- Approval mode: {cfg.approval_mode}

# Tool catalogue
{registry.catalog_text(cfg)}
"""
        return [{"role": "system", "content": SYSTEM_PROMPT + tools_note + "\n" + env}]

    # ------------------------------------------------------------- running
    async def run(self) -> AsyncIterator[dict[str, Any]]:
        """Yields events for the UI and resolves the user's pending message."""
        emit = self.ctx.emit
        cfg = self.ctx.cfg

        for step in range(cfg.max_steps):
            self.ctx.step = step
            if self.ctx.cancel.is_set():
                yield {"type": "cancelled"}
                return

            messages = self._system_messages() + prune(self._history(), cfg.context_budget_tokens)
            schemas = registry.schemas(cfg) if cfg.native_tool_calls else None

            text_parts: list[str] = []
            calls: list[dict] = []
            usage = None
            try:
                async for ev in self.ctx.llm.stream_chat(messages, schemas):
                    if self.ctx.cancel.is_set():
                        yield {"type": "cancelled"}
                        return
                    if ev["type"] == "text":
                        text_parts.append(ev["text"])
                        yield {"type": "text", "text": ev["text"]}
                    elif ev["type"] == "tool_calls":
                        calls = ev["calls"]
                    elif ev["type"] == "usage":
                        usage = ev["usage"]
                    elif ev["type"] == "done":
                        pass
            except LLMError as e:
                if str(e).startswith("TOOLS_UNSUPPORTED::"):
                    yield {"type": "notice",
                           "text": "Model rejected native tools - switching to the text tool protocol."}
                    cfg.save()
                    continue
                yield {"type": "error", "error": str(e)}
                return

            text = "".join(text_parts)

            # Fallback protocol parsing
            if not calls and not cfg.native_tool_calls:
                calls = parse_text_tool_calls(text)

            if not calls:
                if text.strip():
                    self.ctx.store.add_message(self.ctx.chat_id, "assistant", text)
                yield {"type": "final", "text": text, "usage": usage, "steps": step + 1}
                return

            # Persist the assistant turn. In native mode the tool_calls array is
            # part of the message; in text-protocol mode the fenced blocks are
            # already inside `text`, so we store them as plain content.
            self.ctx.store.add_message(self.ctx.chat_id, "assistant", text,
                                       tool_calls=calls if cfg.native_tool_calls else None)
            if text.strip():
                yield {"type": "assistant_text", "text": text}

            for call in calls:
                if self.ctx.cancel.is_set():
                    yield {"type": "cancelled"}
                    return
                name = call["function"]["name"]
                raw_args = call["function"].get("arguments") or "{}"
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except json.JSONDecodeError:
                    yield {"type": "tool", "name": name, "args": raw_args,
                           "result": {"ok": False, "error": "arguments were not valid JSON"},
                           "duration_ms": 0}
                    bad = json.dumps({"ok": False, "error": "arguments were not valid JSON",
                                      "hint": "emit a single valid JSON object in args"})
                    if cfg.native_tool_calls:
                        self.ctx.store.add_message(self.ctx.chat_id, "tool", bad,
                                                   tool_call_id=call["id"], name=name)
                    else:
                        self.ctx.store.add_message(self.ctx.chat_id, "user",
                                                   f"TOOL RESULT ({name}): {bad}")
                    continue

                # approval gate
                tool = registry.get(name)
                need = (cfg.approval_mode == "confirm_all"
                        or (cfg.approval_mode == "confirm_risky"
                            and tool and tool.risk in ("destructive", "shell", "network")))
                if need and self.ctx.approve:
                    approved = await self.ctx.approve(name, args)
                    if not approved:
                        res = {"ok": False, "error": "user denied this action"}
                        yield {"type": "tool", "name": name, "args": args, "result": res,
                               "duration_ms": 0, "denied": True}
                        if cfg.native_tool_calls:
                            self.ctx.store.add_message(self.ctx.chat_id, "tool",
                                                       json.dumps(res),
                                                       tool_call_id=call["id"], name=name)
                        else:
                            self.ctx.store.add_message(self.ctx.chat_id, "user",
                                                       f"TOOL RESULT ({name}): denied by user")
                        continue

                t0 = time.time()
                yield {"type": "tool_start", "name": name, "args": args}
                res = await registry.execute(name, args, self.ctx)
                dur = int((time.time() - t0) * 1000)
                payload = json.dumps(res, default=str)
                if len(payload) > 24000:
                    payload = payload[:24000] + '... [truncated]'
                if cfg.native_tool_calls:
                    self.ctx.store.add_message(self.ctx.chat_id, "tool", payload,
                                               tool_call_id=call["id"], name=name)
                else:
                    # No tool_call_id exists in this protocol; feed the observation
                    # back as a user message so any OpenAI-compatible server accepts
                    # the transcript.
                    self.ctx.store.add_message(
                        self.ctx.chat_id, "user",
                        f"TOOL RESULT ({name}): {payload}")
                yield {"type": "tool", "name": name, "args": args,
                       "result": res, "duration_ms": dur}

            if not cfg.autopilot:
                yield {"type": "paused", "steps": step + 1}
                return

        yield {"type": "limit", "steps": cfg.max_steps}


# ---------------------------------------------------------------------------
# Title generation
# ---------------------------------------------------------------------------
async def suggest_title(llm: LLMClient, first_message: str) -> str:
    text = first_message.strip().replace("\n", " ")[:300]
    try:
        out = []
        async for ev in llm.stream_chat(
                [{"role": "system", "content": "Reply with a 3-6 word title only, no quotes."},
                 {"role": "user", "content": text}], tools=None, max_tokens=24,
                temperature=0.2):
            if ev["type"] == "text":
                out.append(ev["text"])
        title = "".join(out).strip().strip('"').strip()
        return title[:60] or text[:40]
    except Exception:
        return text[:40] or "New chat"
