"""Tool registry: everything the agent can do, declared once with a schema.

Design goals
  * Every tool is a plain function + JSON schema, so it works with native
    OpenAI-style function calling (LM Studio) and with a text fallback.
  * Risky tools are tagged so approval mode can gate them.
  * Tools never throw: they return {"ok":..,"error":..} which the agent reads,
    so a bad call becomes feedback instead of a crashed turn.
"""
from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

Handler = Callable[..., Any]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler
    category: str = "core"
    risk: str = "safe"          # safe | write | destructive | shell | network
    aliases: list[str] = field(default_factory=list)

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": f"[{self.category}] {self.description}",
                "parameters": self.parameters,
            },
        }


class Registry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def add(self, tool: Tool) -> Tool:
        self._tools[tool.name] = tool
        for a in tool.aliases:
            self._tools[a] = tool
        return tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        seen, out = set(), []
        for t in self._tools.values():
            if id(t) not in seen:
                seen.add(id(t))
                out.append(t)
        return out

    def schemas(self, cfg, allowed: set[str] | None = None) -> list[dict[str, Any]]:
        blocked = set(getattr(cfg, "disabled_tools", []) or [])
        out = []
        for t in self.all():
            if t.name in blocked:
                continue
            if allowed is not None and t.name not in allowed:
                continue
            if t.risk == "shell" and not cfg.allow_shell:
                continue
            if t.risk == "network" and not cfg.allow_network:
                continue
            out.append(t.schema())
        return out

    def catalog_text(self, cfg) -> str:
        """Compact tool catalogue for the text-protocol fallback."""
        blocked = set(getattr(cfg, "disabled_tools", []) or [])
        lines: list[str] = []
        by_cat: dict[str, list[Tool]] = {}
        for t in self.all():
            if t.name in blocked:
                continue
            by_cat.setdefault(t.category, []).append(t)
        for cat in sorted(by_cat):
            lines.append(f"## {cat}")
            for t in by_cat[cat]:
                props = t.parameters.get("properties", {})
                req = set(t.parameters.get("required", []))
                args = ", ".join(
                    f"{k}{'' if k in req else '?'}:{v.get('type', 'any')}"
                    for k, v in props.items()
                )
                lines.append(f"- {t.name}({args}) - {t.description.splitlines()[0][:150]}")
        return "\n".join(lines)

    async def execute(self, name: str, args: dict[str, Any], ctx) -> dict[str, Any]:
        tool = self.get(name)
        if tool is None:
            close = [t.name for t in self.all() if name.lower()[:4] in t.name.lower()][:5]
            return {"ok": False, "error": f"unknown tool '{name}'",
                    "hint": f"did you mean: {close}" if close else "see tool list"}
        try:
            result = tool.handler(ctx=ctx, **args)
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, dict):
                return {"ok": not result.get("error"), **result}
            return {"ok": True, "result": result}
        except TypeError as e:
            if "unexpected keyword argument" in str(e) or "missing" in str(e):
                return {"ok": False, "error": f"bad arguments for {name}: {e}",
                        "schema": tool.parameters}
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        except Exception as e:  # noqa: BLE001 - tools must never crash a turn
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}


registry = Registry()


def tool(name: str, description: str, category: str = "core", risk: str = "safe",
         parameters: dict[str, Any] | None = None, aliases: list[str] | None = None):
    """Decorator registering a tool. Handler signature: f(ctx, **kwargs)."""

    def deco(fn: Handler) -> Handler:
        params = parameters or {"type": "object", "properties": {}, "required": []}
        registry.add(Tool(name=name, description=description.strip(), parameters=params,
                          handler=fn, category=category, risk=risk,
                          aliases=aliases or []))
        return fn

    return deco


def P(**props: Any) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": []}


def req(params: dict[str, Any], *names: str) -> dict[str, Any]:
    params = json.loads(json.dumps(params))
    params["required"] = list(names)
    return params


def S(desc: str, enum: list[str] | None = None, default: Any = None) -> dict[str, Any]:
    d: dict[str, Any] = {"type": "string", "description": desc}
    if enum:
        d["enum"] = enum
    if default is not None:
        d["default"] = default
    return d


def N(desc: str, default: Any = None) -> dict[str, Any]:
    d: dict[str, Any] = {"type": "number", "description": desc}
    if default is not None:
        d["default"] = default
    return d


def B(desc: str, default: bool = False) -> dict[str, Any]:
    return {"type": "boolean", "description": desc, "default": default}


def A(desc: str, item_type: str = "string") -> dict[str, Any]:
    return {"type": "array", "description": desc, "items": {"type": item_type}}


def O(desc: str) -> dict[str, Any]:
    return {"type": "object", "description": desc}
