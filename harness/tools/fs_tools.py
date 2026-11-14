"""Filesystem + shell tools: the developer's actual ability to edit the repo.

Path safety: writes are confined to the project folder (plus explicit sandbox
roots) so "full control" doesn't mean "accidentally rewrites system files".
Reads are broader (an agent needs to read engine headers to write correct C++).
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .base import A, B, N, O, P, S, req, tool

TEXT_EXT = {
    ".h", ".hpp", ".cpp", ".c", ".cc", ".cs", ".py", ".ini", ".json", ".uproject",
    ".uplugin", ".txt", ".md", ".xml", ".yaml", ".yml", ".ts", ".js", ".css",
    ".html", ".sh", ".bat", ".ps1", ".hlsl", ".usf", ".ush", ".csv", ".toml",
}
MAX_READ_BYTES = 400_000


def _resolve(ctx, path: str, for_write: bool = False) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute():
        base = ctx.cfg.project_dir or Path.cwd()
        p = base / p
    p = p.resolve()
    if for_write:
        roots = ctx.cfg.writable_roots
        if not any(p == r or r in p.parents for r in roots):
            raise PermissionError(
                f"refusing to write outside the project: {p}\n"
                f"allowed roots: {[str(r) for r in roots]}"
            )
    return p


def _looks_binary(p: Path) -> bool:
    try:
        with p.open("rb") as f:
            return b"\0" in f.read(2048)
    except OSError:
        return True


@tool("fs_read", "Read a text file. Supports line ranges for large files.", "files",
      parameters=req(P(path=S("File path, absolute or relative to the project"),
                      start_line=N("1-based first line", 1),
                      end_line=N("1-based last line (inclusive); 0 = end", 0)), "path"))
def fs_read(ctx, path: str, start_line: int = 1, end_line: int = 0):
    p = _resolve(ctx, path)
    if not p.exists():
        return {"error": f"not found: {p}"}
    if p.is_dir():
        return {"error": f"{p} is a directory; use fs_list"}
    if _looks_binary(p):
        return {"error": f"binary file ({p.stat().st_size} bytes): {p.name}",
                "hint": "use ue_asset_info or fs_glob for binaries"}
    if p.stat().st_size > MAX_READ_BYTES and end_line == 0:
        end_line = start_line + 1500
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    end = end_line if end_line else len(lines)
    chunk = lines[max(0, start_line - 1):end]
    body = "\n".join(chunk)
    header = f"{p} [lines {start_line}-{min(end, len(lines))} of {len(lines)}]"
    return {"path": str(p), "total_lines": len(lines), "content": body,
            "note": header if len(lines) > len(chunk) else f"{p}"}


@tool("fs_write", "Create or overwrite a text file (creates parent folders).", "files",
      risk="write",
      parameters=req(P(path=S("File path"), content=S("Full file content"),
                       append=B("Append instead of overwrite")), "path", "content"))
def fs_write(ctx, path: str, content: str, append: bool = False):
    p = _resolve(ctx, path, for_write=True)
    p.parent.mkdir(parents=True, exist_ok=True)
    existed = p.exists()
    with p.open("a" if append else "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    return {"path": str(p), "bytes": len(content.encode()), "created": not existed}


@tool("fs_patch", "Replace an exact snippet in a file (surgical edit, like a diff hunk).",
      "files", risk="write",
      parameters=req(P(path=S("File path"), old=S("Exact text to find"),
                       new=S("Replacement text"), count=N("Max replacements (0=all)", 1)),
                     "path", "old", "new"))
def fs_patch(ctx, path: str, old: str, new: str, count: int = 1):
    p = _resolve(ctx, path, for_write=True)
    if not p.exists():
        return {"error": f"not found: {p}"}
    text = p.read_text(encoding="utf-8", errors="replace")
    if old not in text:
        # give a useful near-miss hint
        first = old.strip().splitlines()[0] if old.strip() else ""
        near = [i + 1 for i, l in enumerate(text.splitlines()) if first and first[:40] in l]
        return {"error": "snippet not found", "near_miss_lines": near[:5],
                "hint": "read the file again and copy the exact text including indentation"}
    n = text.count(old)
    replaced = text.replace(old, new, count if count > 0 else -1)
    p.write_text(replaced, encoding="utf-8", newline="\n")
    return {"path": str(p), "occurrences": n, "replaced": count if count > 0 else n}


@tool("fs_list", "List a directory with sizes and types.", "files",
      parameters=req(P(path=S("Directory (default: project root)", default="."),
                       depth=N("Recursion depth", 1),
                       glob=S("Optional glob filter, e.g. *.uasset")), ))
def fs_list(ctx, path: str = ".", depth: int = 1, glob: str = ""):
    p = _resolve(ctx, path)
    if not p.exists():
        return {"error": f"not found: {p}"}
    entries = []
    root_depth = len(p.parts)
    for cur, dirs, files in os.walk(p):
        cur_p = Path(cur)
        if len(cur_p.parts) - root_depth >= max(0, depth):
            dirs[:] = []
        dirs[:] = [d for d in dirs if d not in
                   {"Intermediate", "Saved", "DerivedDataCache", "Binaries", ".git"}]
        for d in dirs:
            entries.append({"type": "dir", "path": str(cur_p / d)})
        for f in files:
            if glob and not fnmatch.fnmatch(f, glob):
                continue
            fp = cur_p / f
            try:
                size = fp.stat().st_size
            except OSError:
                size = 0
            entries.append({"type": "file", "path": str(fp), "size": size})
    return {"root": str(p), "count": len(entries), "entries": entries[:800]}


@tool("fs_glob", "Find files by glob pattern across the project (e.g. **/*.uasset).",
      "files", parameters=req(P(pattern=S("Glob pattern"), limit=N("Max results", 200)), "pattern"))
def fs_glob(ctx, pattern: str, limit: int = 200):
    base = ctx.cfg.project_dir or Path.cwd()
    hits = [str(p) for p in base.glob(pattern)][:limit]
    return {"pattern": pattern, "count": len(hits), "files": hits}


@tool("fs_grep", "Search file contents with a regex (ripgrep-style). Returns file:line: text.",
      "files", parameters=req(P(pattern=S("Regex pattern"),
                                path=S("Directory or file", default="."),
                                include=S("Glob filter, e.g. *.cpp"),
                                limit=N("Max matches", 120),
                                ignore_case=B("Case insensitive", True)), "pattern"))
def fs_grep(ctx, pattern: str, path: str = ".", include: str = "", limit: int = 120,
            ignore_case: bool = True):
    root = _resolve(ctx, path)
    flags = re.IGNORECASE if ignore_case else 0
    try:
        rx = re.compile(pattern, flags)
    except re.error as e:
        return {"error": f"bad regex: {e}"}
    files: list[Path] = []
    if root.is_file():
        files = [root]
    else:
        for cur, dirs, fs in os.walk(root):
            dirs[:] = [d for d in dirs if d not in
                       {"Intermediate", "Saved", "DerivedDataCache", "Binaries", ".git", "node_modules"}]
            for f in fs:
                if include and not fnmatch.fnmatch(f, include):
                    continue
                fp = Path(cur) / f
                if fp.suffix.lower() not in TEXT_EXT:
                    continue
                files.append(fp)
    matches = []
    for fp in files:
        try:
            for i, line in enumerate(fp.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                if rx.search(line):
                    matches.append(f"{fp}:{i}: {line.strip()[:220]}")
                    if len(matches) >= limit:
                        break
        except OSError:
            continue
        if len(matches) >= limit:
            break
    return {"pattern": pattern, "count": len(matches), "matches": matches}


@tool("fs_mkdir", "Create a directory tree.", "files", risk="write",
      parameters=req(P(path=S("Directory path")), "path"))
def fs_mkdir(ctx, path: str):
    p = _resolve(ctx, path, for_write=True)
    p.mkdir(parents=True, exist_ok=True)
    return {"path": str(p)}


@tool("fs_move", "Move or rename a file/directory.", "files", risk="destructive",
      parameters=req(P(src=S("Source"), dst=S("Destination")), "src", "dst"))
def fs_move(ctx, src: str, dst: str):
    a, b = _resolve(ctx, src, True), _resolve(ctx, dst, True)
    b.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(a), str(b))
    return {"from": str(a), "to": str(b)}


@tool("fs_delete", "Delete a file or directory (recursive).", "files", risk="destructive",
      parameters=req(P(path=S("Path to delete")), "path"))
def fs_delete(ctx, path: str):
    p = _resolve(ctx, path, for_write=True)
    if p.is_dir():
        shutil.rmtree(p)
        return {"deleted_dir": str(p)}
    p.unlink()
    return {"deleted": str(p)}


@tool("shell_run", "Run a shell command in the project (build tools, git, UBT, UAT, Live Coding).",
      "system", risk="shell",
      parameters=req(P(command=S("Command line"), cwd=S("Working directory (default: project)"),
                       timeout=N("Timeout seconds", 300),
                       env=O("Extra environment variables")), "command"))
def shell_run(ctx, command: str, cwd: str = "", timeout: int = 300, env: dict | None = None):
    workdir = _resolve(ctx, cwd) if cwd else (ctx.cfg.project_dir or Path.cwd())
    full_env = os.environ.copy()
    if env:
        full_env.update({k: str(v) for k, v in env.items()})
    t0 = time.time()
    try:
        proc = subprocess.run(command, shell=True, cwd=str(workdir), env=full_env,
                              capture_output=True, text=True, timeout=timeout,
                              errors="replace")
    except subprocess.TimeoutExpired as e:
        return {"error": f"timed out after {timeout}s", "stdout": (e.stdout or "")[-4000:]}
    out = proc.stdout[-12000:]
    err = proc.stderr[-6000:]
    return {"exit_code": proc.returncode, "stdout": out, "stderr": err,
            "duration_ms": int((time.time() - t0) * 1000),
            "ok": proc.returncode == 0}


@tool("http_request", "HTTP request to any URL (docs, APIs). Off by default.", "system",
      risk="network",
      parameters=req(P(url=S("URL"), method=S("HTTP method", ["GET", "POST", "PUT", "DELETE"], "GET"),
                       body=S("Request body"), headers=O("Headers")), "url"))
def http_request(ctx, url: str, method: str = "GET", body: str = "", headers: dict | None = None):
    import httpx
    r = httpx.request(method, url, content=body or None, headers=headers or {},
                      timeout=30.0, follow_redirects=True)
    return {"status": r.status_code, "body": r.text[:20000],
            "headers": dict(r.headers)}
