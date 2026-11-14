"""Configuration storage for Unreal Harness.

Everything lives under a single app-data directory so an uninstall is a single
`rm -rf` (or uninstall.ps1). No registry keys, no system-wide pollution.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any


def app_dir() -> Path:
    """Per-user data directory (config, chats, logs, venv-independent)."""
    override = os.environ.get("UNREAL_HARNESS_HOME")
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return (base / "UnrealHarness").resolve()


DATA_DIR = app_dir()
CONFIG_PATH = DATA_DIR / "config.json"
LOG_DIR = DATA_DIR / "logs"
CHAT_DB = DATA_DIR / "chats.db"


@dataclass
class Config:
    # ---- LLM (LM Studio by default: OpenAI-compatible on :1234) ----
    llm_base_url: str = "http://127.0.0.1:1234/v1"
    llm_api_key: str = "lm-studio"
    model: str = ""                     # empty => first model LM Studio reports
    temperature: float = 0.2
    max_tokens: int = 4096
    context_budget_tokens: int = 24576  # prune history above this
    native_tool_calls: bool = True      # use OpenAI `tools`; fall back to text protocol

    # ---- Unreal ----
    project_path: str = ""              # path to .uproject (or its folder)
    engine_path: str = ""               # UE install root, e.g. C:/Program Files/Epic Games/UE_5.5
    rc_host: str = "127.0.0.1"
    rc_port: int = 30010                # Remote Control API (stock plugin)
    bridge_host: str = "127.0.0.1"
    bridge_port: int = 34567            # in-editor fast lane
    prefer_transport: str = "auto"      # auto | bridge | rc | disk
    auto_install_bridge: bool = True    # push bootstrap python into the editor on connect

    # ---- Harness ----
    host: str = "127.0.0.1"
    port: int = 8787
    approval_mode: str = "auto"         # auto | confirm_risky | confirm_all
    autopilot: bool = True              # keep looping until the task is done
    max_steps: int = 60                 # tool-call rounds per user message
    allow_shell: bool = True
    allow_network: bool = False
    sandbox_paths: list[str] = field(default_factory=list)  # extra writable roots
    disabled_tools: list[str] = field(default_factory=list)
    telemetry: bool = False
    theme: str = "dark"

    # ---- Updates ----
    update_channel: str = "main"
    repo_url: str = "https://github.com/Arvex-GH/Unreal-Harness"
    check_updates_on_start: bool = True
    last_update_check: float = 0.0

    # ---- runtime (not persisted) ----
    _extra: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def load(cls) -> "Config":
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        cfg = cls()
        if CONFIG_PATH.exists():
            try:
                raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except Exception:
                raw = {}
            known = {f.name for f in fields(cls) if not f.name.startswith("_")}
            for k, v in raw.items():
                if k in known:
                    setattr(cfg, k, v)
        if cfg.repo_url.rstrip("/").removesuffix(".git") in (
            "",
            "https://github.com/unreal-harness/unreal-harness",
        ):
            cfg.repo_url = "https://github.com/Arvex-GH/Unreal-Harness"
        cfg._autodetect()
        return cfg

    def _autodetect(self) -> None:
        """Best-effort discovery so a fresh install usually just works."""
        if not self.project_path:
            found = discover_uproject()
            if found:
                self.project_path = str(found)
        if not self.engine_path:
            found = discover_engine(self.project_path)
            if found:
                self.engine_path = str(found)

    def save(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        payload = {k: v for k, v in asdict(self).items() if not k.startswith("_")}
        tmp = CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(CONFIG_PATH)

    # convenience -------------------------------------------------------
    @property
    def project_file(self) -> Path | None:
        if not self.project_path:
            return None
        p = Path(self.project_path)
        if p.is_dir():
            cands = sorted(p.glob("*.uproject"))
            return cands[0] if cands else None
        return p if p.suffix == ".uproject" else None

    @property
    def project_dir(self) -> Path | None:
        f = self.project_file
        return f.parent if f else None

    @property
    def writable_roots(self) -> list[Path]:
        roots: list[Path] = []
        if self.project_dir:
            roots.append(self.project_dir)
        roots += [Path(p).expanduser() for p in self.sandbox_paths]
        roots.append(DATA_DIR / "workspace")
        seen, out = set(), []
        for r in roots:
            rp = r.resolve()
            if rp not in seen:
                seen.add(rp)
                out.append(rp)
        return out

    def public(self) -> dict[str, Any]:
        d = {k: v for k, v in asdict(self).items() if not k.startswith("_")}
        d["project_file"] = str(self.project_file) if self.project_file else ""
        d["data_dir"] = str(DATA_DIR)
        return d


# ---------------------------------------------------------------------------
# discovery helpers
# ---------------------------------------------------------------------------
_SKIP_DIRS = {".git", "node_modules", "Intermediate", "Saved", "DerivedDataCache",
              "Binaries", "Build", ".vs", "__pycache__", "Content"}


def discover_uproject(start: Path | None = None, depth: int = 3) -> Path | None:
    """Walk up, then down a little, looking for a single .uproject."""
    starts = [start] if start else [Path.cwd(), Path.home(), Path.home() / "Documents"]
    for s in starts:
        if not s:
            continue
        s = Path(s).resolve()
        for parent in [s, *s.parents]:
            hits = list(parent.glob("*.uproject"))
            if len(hits) == 1:
                return hits[0]
            if hits:
                return sorted(hits)[0]
            if parent.name.lower() in {"unreal projects", "documents"}:
                for sub in parent.iterdir() if parent.exists() else []:
                    if sub.is_dir():
                        h = list(sub.glob("*.uproject"))
                        if h:
                            return h[0]
    return None


def discover_engine(project_path: str = "") -> Path | None:
    """Find a UE install from the .uproject association or the default install dirs."""
    if project_path:
        try:
            data = json.loads(Path(project_path).read_text(encoding="utf-8-sig"))
            assoc = data.get("EngineAssociation", "")
            if assoc and not assoc.startswith("{") and len(assoc) > 3:
                p = Path(assoc)
                if p.exists():
                    return p
                # registry-style version => probe standard locations
                cand = _probe_engine_versions(assoc)
                if cand:
                    return cand
        except Exception:
            pass
    return _probe_engine_versions(None)


def _probe_engine_versions(version: str | None) -> Path | None:
    roots: list[Path] = []
    if sys.platform == "win32":
        for env in ("ProgramFiles", "ProgramFiles(x86)"):
            base = os.environ.get(env)
            if base:
                roots.append(Path(base) / "Epic Games")
    elif sys.platform == "darwin":
        roots.append(Path("/Users/Shared/Epic Games"))
    else:
        roots += [Path("/opt/UnrealEngine"), Path.home() / "UnrealEngine",
                  Path("/usr/local/UnrealEngine")]
    for root in roots:
        if not root.exists():
            continue
        cands = [d for d in root.iterdir() if d.is_dir() and d.name.startswith("UE_")]
        if version:
            exact = [d for d in cands if version.replace(".", "") in d.name.replace(".", "")]
            if exact:
                return exact[0]
        if cands:
            return sorted(cands, key=lambda d: d.name)[-1]
    return None


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "workspace").mkdir(parents=True, exist_ok=True)
