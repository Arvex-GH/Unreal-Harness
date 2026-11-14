"""Unreal Harness - local-first AI agent with full control over Unreal Engine 5."""

from pathlib import Path

__all__ = ["__version__", "ROOT"]

ROOT = Path(__file__).resolve().parent.parent


def _read_version() -> str:
    try:
        return (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    except Exception:
        return "0.0.0"


__version__ = _read_version()
