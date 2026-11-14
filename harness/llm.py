"""LLM client: OpenAI-compatible, defaults tuned for LM Studio.

LM Studio exposes /v1/chat/completions and supports OpenAI-style `tools`.
Not every local model does tool calling well, so we detect failure and fall back
to a strict text protocol that we parse ourselves.
"""
from __future__ import annotations

import json
import re
from typing import Any, AsyncIterator

import httpx

from .config import Config


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0))

    # ------------------------------------------------------------------ util
    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.cfg.llm_api_key:
            h["Authorization"] = f"Bearer {self.cfg.llm_api_key}"
        return h

    async def list_models(self) -> list[dict[str, Any]]:
        try:
            r = await self._client.get(f"{self.cfg.llm_base_url}/models",
                                       headers=self._headers(), timeout=8.0)
            r.raise_for_status()
            data = r.json()
            return data.get("data", []) if isinstance(data, dict) else []
        except Exception as e:  # noqa: BLE001
            raise LLMError(
                f"Cannot reach LM Studio at {self.cfg.llm_base_url}: {e}. "
                "Start LM Studio, go to the Developer tab, and press 'Start Server'.") from e

    async def health(self) -> dict[str, Any]:
        try:
            models = await self.list_models()
            ids = [m.get("id") for m in models]
            return {"ok": True, "models": ids, "base_url": self.cfg.llm_base_url,
                    "selected": self.cfg.model or (ids[0] if ids else None)}
        except LLMError as e:
            return {"ok": False, "error": str(e), "base_url": self.cfg.llm_base_url}

    async def resolve_model(self) -> str:
        if self.cfg.model:
            return self.cfg.model
        models = await self.list_models()
        if not models:
            raise LLMError("LM Studio reports no loaded models. Load a model first.")
        self.cfg.model = models[0]["id"]
        try:
            self.cfg.save()
        except Exception:
            pass
        return self.cfg.model

    # ------------------------------------------------------------------ chat
    async def stream_chat(self, messages: list[dict], tools: list[dict] | None = None,
                          temperature: float | None = None,
                          max_tokens: int | None = None) -> AsyncIterator[dict[str, Any]]:
        """Yields events: {'type':'text','text':..}, {'type':'tool_calls','calls':..},
        {'type':'usage','usage':..}, {'type':'done'}."""
        model = await self.resolve_model()
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": self.cfg.temperature if temperature is None else temperature,
            "max_tokens": self.cfg.max_tokens if max_tokens is None else max_tokens,
            "stream": True,
        }
        if tools and self.cfg.native_tool_calls:
            body["tools"] = tools
            body["tool_choice"] = "auto"

        url = f"{self.cfg.llm_base_url}/chat/completions"
        acc: list[dict] = []
        text_buf: list[str] = []
        usage: dict[str, Any] | None = None

        try:
            async with self._client.stream("POST", url, json=body, headers=self._headers()) as r:
                if r.status_code >= 400:
                    raw = (await r.aread()).decode("utf-8", "replace")
                    if body.get("tools") and ("tool" in raw.lower() or "400" in str(r.status_code)):
                        # Model/server refuses tools, or emitted a tool-related 400:
                        # retry once without them so we fall back to text protocol.
                        self.cfg.native_tool_calls = False
                        raise LLMError(f"TOOLS_UNSUPPORTED::{raw[:400]}")
                    raise LLMError(f"LLM {r.status_code}: {raw[:800]}")

                async for line in r.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    for choice in chunk.get("choices", []):
                        delta = choice.get("delta") or {}
                        if delta.get("content"):
                            text_buf.append(delta["content"])
                            yield {"type": "text", "text": delta["content"]}
                        for tc in delta.get("tool_calls") or []:
                            idx = tc.get("index", 0)
                            while len(acc) <= idx:
                                acc.append({"id": "", "type": "function",
                                            "function": {"name": "", "arguments": ""}})
                            if tc.get("id"):
                                acc[idx]["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                acc[idx]["function"]["name"] += fn["name"]
                            if fn.get("arguments"):
                                acc[idx]["function"]["arguments"] += fn["arguments"]
        except httpx.HTTPError as e:
            raise LLMError(f"LLM connection error: {e}") from e

        clean_calls = [c for c in acc if c["function"]["name"]]
        for i, c in enumerate(clean_calls):
            if not c["id"]:
                c["id"] = f"call_{i}"
        if clean_calls:
            yield {"type": "tool_calls", "calls": clean_calls}
        if usage:
            yield {"type": "usage", "usage": usage}
        yield {"type": "done", "text": "".join(text_buf)}


# ---------------------------------------------------------------------------
# Text protocol fallback
# ---------------------------------------------------------------------------
TOOL_CALL_BLOCK = re.compile(r"```(?:tool_call|json)\s*(.*?)```", re.S)


def parse_text_tool_calls(text: str) -> list[dict[str, Any]]:
    """Parse the fallback protocol:
    ```tool_call
    {"tool": "bp_create", "args": {"path": "/Game/BP_X"}}
    ```
    Also accepts {"name":..., "arguments":...} and a bare list of calls.
    """
    calls: list[dict[str, Any]] = []
    for block in TOOL_CALL_BLOCK.findall(text):
        block = block.strip()
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            # tolerate trailing commas / single quotes from sloppy models
            try:
                data = json.loads(block.replace(",}", "}").replace(",]", "]"))
            except json.JSONDecodeError:
                continue
        items = data if isinstance(data, list) else [data]
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            name = item.get("tool") or item.get("name") or item.get("function")
            args = item.get("args") or item.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            if name:
                calls.append({"id": f"txt_{len(calls)}", "type": "function",
                              "function": {"name": name,
                                           "arguments": json.dumps(args)}})
    return calls
