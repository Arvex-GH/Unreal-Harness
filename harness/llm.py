"""LLM client: OpenAI-compatible, defaults tuned for LM Studio.

LM Studio exposes /v1/chat/completions and supports OpenAI-style `tools`.
Not every local model does tool calling well, so we detect failure (both HTTP
errors and mid-stream SSE engine errors) and fall back to a strict text protocol
that we parse ourselves.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any, AsyncIterator

import httpx

from .config import Config


class LLMError(RuntimeError):
    pass


def _is_embedding_model(model_id: str) -> bool:
    low = (model_id or "").lower()
    return "embed" in low or low.startswith("text-embedding")


def _extract_error_message(payload: Any) -> str:
    """Pull a readable error message out of OpenAI / LM Studio / llama.cpp payloads."""
    if isinstance(payload, str):
        stripped = payload.strip()
        if stripped.startswith("{"):
            try:
                return _extract_error_message(json.loads(stripped))
            except json.JSONDecodeError:
                pass
        return stripped
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            msg = err.get("message") or err.get("error")
            if msg:
                return _extract_error_message(msg)
            return json.dumps(err)
        if isinstance(err, str) and err.strip():
            return _extract_error_message(err)
        if isinstance(payload.get("message"), str) and payload["message"].strip():
            return _extract_error_message(payload["message"])
        return json.dumps(payload)
    return str(payload)


def _is_tools_error(status_code: int, message: str) -> bool:
    """Return True if an LLM error is likely caused by passing native `tools`."""
    low = (message or "").lower()
    if any(fatal in low for fatal in ("no models loaded", "model not found", "not loaded")):
        return False
    if status_code in (400, 422):
        return True
    return any(
        kw in low
        for kw in (
            "tool",
            "function",
            "json schema",
            "schema error",
            "grammar",
            "jinja",
            "template",
            "context size",
            "context length",
            "n_ctx",
            "exceeded",
        )
    )


class LLMClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._client: httpx.AsyncClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._no_tools_models: set[str] = set()

    def _get_client(self) -> httpx.AsyncClient:
        loop = asyncio.get_running_loop()
        if self._client is None or self._client.is_closed or self._loop is not loop:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0))
            self._loop = loop
        return self._client

    # ------------------------------------------------------------------ util
    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.cfg.llm_api_key:
            h["Authorization"] = f"Bearer {self.cfg.llm_api_key}"
        return h

    def supports_native_tools(self, model: str = "") -> bool:
        if not self.cfg.native_tool_calls:
            return False
        if model and model in self._no_tools_models:
            return False
        return True

    def mark_tools_unsupported(self, model: str = "") -> None:
        if model:
            self._no_tools_models.add(model)
        else:
            self.cfg.native_tool_calls = False

    async def list_models(self) -> list[dict[str, Any]]:
        try:
            r = await self._get_client().get(f"{self.cfg.llm_base_url}/models",
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
            ids = [m.get("id") for m in models if m.get("id")]
            chat_ids = [mid for mid in ids if not _is_embedding_model(mid)]
            default_id = chat_ids[0] if chat_ids else (ids[0] if ids else None)
            return {"ok": True, "models": ids, "chat_models": chat_ids,
                    "base_url": self.cfg.llm_base_url,
                    "selected": self.cfg.model or default_id}
        except LLMError as e:
            return {"ok": False, "error": str(e), "base_url": self.cfg.llm_base_url}

    async def resolve_model(self) -> str:
        if self.cfg.model:
            return self.cfg.model
        models = await self.list_models()
        if not models:
            raise LLMError("LM Studio reports no loaded models. Load a model first.")
        ids = [m["id"] for m in models if m.get("id")]
        chat_ids = [mid for mid in ids if not _is_embedding_model(mid)]
        if not chat_ids and not ids:
            raise LLMError("LM Studio reports no usable models. Load a chat/LLM model first.")
        return chat_ids[0] if chat_ids else ids[0]

    # ------------------------------------------------------------------ chat
    async def stream_chat(self, messages: list[dict], tools: list[dict] | None = None,
                          temperature: float | None = None,
                          max_tokens: int | None = None) -> AsyncIterator[dict[str, Any]]:
        """Yields events: {'type':'text','text':..}, {'type':'tool_calls','calls':..},
        {'type':'usage','usage':..}, {'type':'done'}."""
        model = await self.resolve_model()
        use_tools = bool(tools and self.supports_native_tools(model))
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": self.cfg.temperature if temperature is None else temperature,
            "max_tokens": self.cfg.max_tokens if max_tokens is None else max_tokens,
            "stream": True,
        }
        if use_tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"

        url = f"{self.cfg.llm_base_url}/chat/completions"
        acc: list[dict] = []
        text_buf: list[str] = []
        reasoning_buf: list[str] = []
        usage: dict[str, Any] | None = None
        stream_error: str | None = None
        raw_non_sse: list[str] = []
        event_type = ""

        try:
            async with self._get_client().stream("POST", url, json=body, headers=self._headers()) as r:
                if r.status_code >= 400:
                    raw = (await r.aread()).decode("utf-8", "replace")
                    msg = _extract_error_message(raw)
                    if use_tools and _is_tools_error(r.status_code, raw):
                        self.mark_tools_unsupported(model)
                        raise LLMError(f"TOOLS_UNSUPPORTED::{msg[:400]}")
                    raise LLMError(f"LLM {r.status_code}: {msg[:800]}")

                async for line in r.aiter_lines():
                    stripped = line.strip()
                    if not stripped:
                        event_type = ""
                        continue
                    if stripped.startswith(":"):
                        continue
                    if stripped.startswith("event:"):
                        event_type = stripped[6:].strip().lower()
                        continue
                    if not stripped.startswith("data:"):
                        raw_non_sse.append(stripped)
                        continue

                    data = stripped[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        if event_type == "error" and data:
                            stream_error = data
                            break
                        continue

                    if isinstance(chunk, dict) and (chunk.get("error") or event_type == "error"):
                        stream_error = _extract_error_message(chunk)
                        break

                    if not isinstance(chunk, dict):
                        continue
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    for choice in chunk.get("choices", []):
                        delta = choice.get("delta") or choice.get("message") or {}
                        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                        if reasoning:
                            reasoning_buf.append(reasoning)
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

        if not stream_error and raw_non_sse and not text_buf and not acc:
            joined = "\n".join(raw_non_sse)
            try:
                parsed = json.loads(joined)
                if isinstance(parsed, dict) and (parsed.get("error") or parsed.get("message")):
                    stream_error = _extract_error_message(parsed)
            except json.JSONDecodeError:
                pass

        if stream_error:
            if use_tools and _is_tools_error(500, stream_error):
                self.mark_tools_unsupported(model)
                raise LLMError(f"TOOLS_UNSUPPORTED::{stream_error[:400]}")
            raise LLMError(f"LM Studio error: {stream_error[:800]}")

        clean_calls = [c for c in acc if c["function"]["name"]]
        for i, c in enumerate(clean_calls):
            if not c["id"]:
                c["id"] = f"call_{i}"
        if clean_calls:
            yield {"type": "tool_calls", "calls": clean_calls}
        if usage:
            yield {"type": "usage", "usage": usage}
        yield {"type": "done", "text": "".join(text_buf),
               "reasoning": "".join(reasoning_buf)}


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
        for item in items:
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
