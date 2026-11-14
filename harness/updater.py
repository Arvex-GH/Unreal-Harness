"""Self-update: git pull when installed from a checkout, release download otherwise.

Safety rules:
  * never touch the user's chats/config (they live in the app-data dir, not the repo)
  * refuse to update with local uncommitted changes unless forced
  * record every update in update_history.json so a bad version can be rolled back
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

import httpx

from . import ROOT, _read_version
from .config import DATA_DIR, Config

HISTORY = DATA_DIR / "update_history.json"
_LAST_CHECK: dict[str, Any] | None = None


def _git(*args: str, cwd: Path | None = None) -> tuple[int, str]:
    try:
        p = subprocess.run(["git", *args], cwd=str(cwd or ROOT), capture_output=True,
                           text=True, timeout=60)
        return p.returncode, (p.stdout + p.stderr).strip()
    except Exception as e:  # noqa: BLE001
        return 1, str(e)


def is_git_checkout() -> bool:
    return (ROOT / ".git").exists() and shutil.which("git") is not None


def history() -> list[dict[str, Any]]:
    if HISTORY.exists():
        try:
            return json.loads(HISTORY.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def _record(entry: dict[str, Any]) -> None:
    h = history()
    h.insert(0, entry)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY.write_text(json.dumps(h[:50], indent=2), encoding="utf-8")


def _repo_slug(repo_url: str) -> str:
    s = (repo_url or "").strip().rstrip("/")
    if s.endswith(".git"):
        s = s[:-4]
    if "github.com:" in s and "://" not in s:
        s = s.split("github.com:", 1)[-1]
    elif "github.com/" in s:
        s = s.split("github.com/", 1)[-1]
    return s.strip("/")


def _latest_release(cfg: Config) -> tuple[dict[str, Any] | None, str | None]:
    """Ask GitHub for the newest published release of the configured repo."""
    slug = _repo_slug(cfg.repo_url)
    if not slug or "/" not in slug:
        return None, f"invalid GitHub repo_url: {cfg.repo_url!r}"
    cur = _read_version()
    try:
        api = f"https://api.github.com/repos/{slug}/releases/latest"
        r = httpx.get(
            api,
            timeout=10.0,
            follow_redirects=True,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": f"Unreal-Harness/{cur}",
            },
        )
        if r.status_code == 404:
            return None, f"no releases published yet on {slug}"
        if r.status_code != 200:
            return None, f"GitHub release API returned HTTP {r.status_code}"
        data = r.json()
        zip_url = ""
        for asset in data.get("assets") or []:
            name = (asset.get("name") or "").lower()
            if name.endswith(".zip") and asset.get("browser_download_url"):
                zip_url = asset["browser_download_url"]
                break
        if not zip_url:
            zip_url = data.get("zipball_url") or ""
        return {
            "tag": data.get("tag_name", "").lstrip("v"),
            "zip": zip_url,
            "zipball": data.get("zipball_url") or "",
            "notes": (data.get("body") or "")[:2000],
            "published": data.get("published_at", ""),
        }, None
    except Exception as e:  # noqa: BLE001
        return None, f"could not reach GitHub release API: {e}"


async def check(cfg: Config, force: bool = False) -> dict[str, Any]:
    global _LAST_CHECK
    current = _read_version()
    base = {"current": current, "channel": cfg.update_channel,
            "mode": "git" if is_git_checkout() else "release"}
    if not force and _LAST_CHECK and time.time() - (cfg.last_update_check or 0) < 300:
        return {**_LAST_CHECK, "current": current, "cached": True}

    if is_git_checkout():
        rc, out = _git("fetch", "--all", "--prune")
        if rc != 0:
            return {**base, "error": out, "update_available": False}

        # Resolve the upstream ref explicitly: a fresh clone may only have `main`
        # while the local branch tracks something else (or nothing at all).
        rc_up, upstream = _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
        has_upstream = rc_up == 0 and "/" in upstream and "fatal" not in upstream
        if not has_upstream:
            for cand in (f"origin/{cfg.update_channel}", "origin/main", "origin/master"):
                rc_c, _ = _git("rev-parse", "--verify", "--quiet", cand)
                if rc_c == 0:
                    upstream = cand
                    has_upstream = True
                    break

        rc2, local = _git("rev-parse", "--short", "HEAD")
        local = local if rc2 == 0 and "fatal" not in local else "(no commits yet)"
        rc4, status = _git("status", "--porcelain")

        if not has_upstream:
            cfg.last_update_check = time.time()
            cfg.save()
            res = {**base, "local": local, "dirty": bool(status.strip()),
                   "update_available": False,
                   "hint": "no upstream branch configured; "
                           f"git branch --set-upstream-to=origin/{cfg.update_channel}"}
            _LAST_CHECK = res
            return res

        rc, behind = _git("rev-list", "--count", f"HEAD..{upstream}")
        rc3, remote = _git("rev-parse", "--short", upstream)
        remote = remote if rc3 == 0 and "fatal" not in remote else ""
        cfg.last_update_check = time.time()
        cfg.save()
        try:
            n = int(behind.strip() or 0)
        except ValueError:
            n = 0
        res = {**base, "commits_behind": n, "local": local, "remote": remote,
               "upstream": upstream,
               "dirty": bool(status.strip()), "update_available": n > 0,
               "notes": _git("log", "--oneline", "-n", str(max(n, 1)),
                             f"HEAD..{upstream}")[1]}
        _LAST_CHECK = res
        return res

    rel, err = _latest_release(cfg)
    cfg.last_update_check = time.time()
    cfg.save()
    if not rel:
        res = {**base, "error": err or "could not reach the release API",
               "update_available": False}
        _LAST_CHECK = res
        return res
    latest = rel["tag"]
    res = {**base, "latest": latest, "notes": rel["notes"],
           "update_available": _newer(latest, current), "download": rel["zip"]}
    _LAST_CHECK = res
    return res


def _newer(a: str, b: str) -> bool:
    def parts(v: str) -> list[int]:
        return [int(x) for x in v.replace("-", ".").split(".") if x.isdigit()][:4]
    try:
        return parts(a) > parts(b)
    except Exception:
        return a != b


def apply(cfg: Config, force: bool = False) -> dict[str, Any]:
    """Perform the update. Returns a structured report."""
    global _LAST_CHECK
    before = _read_version()
    if is_git_checkout():
        rc, status = _git("status", "--porcelain")
        if status.strip() and not force:
            return {"ok": False, "error": "local changes present - commit or stash first",
                    "dirty_files": status.splitlines()[:20],
                    "hint": "pass force to discard local changes (config and chats are kept)"}
        rc, out = _git("rev-parse", "--short", "HEAD")
        prev = out.strip()
        if force:
            _git("stash", "push", "-u", "-m", "unreal-harness: pre-update autostash")
        rc, pull = _git("pull", "--ff-only", "origin", cfg.update_channel)
        if rc != 0:
            rc, pull = _git("pull", "origin", cfg.update_channel)
        if rc != 0:
            return {"ok": False, "error": pull, "mode": "git"}
        rc, now = _git("rev-parse", "--short", "HEAD")
        entry = {"at": time.time(), "from": prev, "to": now.strip(),
                 "mode": "git", "ok": True, "log": pull[:4000]}
        _record(entry)
        _LAST_CHECK = None
        return {"ok": True, "mode": "git", "previous": prev, "current": now.strip(),
                "needs_restart": True, "details": pull[:4000]}

    rel, err = _latest_release(cfg)
    if not rel or not rel.get("zip"):
        return {"ok": False, "error": err or "no release available"}
    if not _newer(rel["tag"], before) and not force:
        return {"ok": False, "error": f"already up to date ({before})"}

    with tempfile.TemporaryDirectory() as td:
        zip_path = Path(td) / "release.zip"
        headers = {"User-Agent": f"Unreal-Harness/{before}"}
        with httpx.stream("GET", rel["zip"], follow_redirects=True,
                          timeout=120, headers=headers) as r:
            r.raise_for_status()
            with zip_path.open("wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
        extract_dir = Path(td) / "extracted"
        extract_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(extract_dir)
        roots = [p for p in extract_dir.iterdir() if p.is_dir() and p.name != "__MACOSX"]
        src = roots[0] if len(roots) == 1 and not (extract_dir / "pyproject.toml").exists() else extract_dir

        if not (src / "harness").is_dir():
            return {"ok": False, "error": "downloaded archive does not contain harness/ package"}

        backup = DATA_DIR / "backups" / f"v{before}-{int(time.time())}"
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(ROOT, backup, ignore=shutil.ignore_patterns(
            ".git", "__pycache__", "*.pyc", ".venv", "node_modules", "releases"))

        keep = {"config.json", "chats.db", "logs", "update_history.json", "workspace", ".venv", ".git"}
        for item in src.iterdir():
            if item.name in keep:
                continue
            dest = ROOT / item.name
            if item.is_dir():
                if dest.exists():
                    shutil.rmtree(dest, ignore_errors=True)
                shutil.copytree(item, dest)
            else:
                if dest.is_dir():
                    shutil.rmtree(dest, ignore_errors=True)
                shutil.copy2(item, dest)

    entry = {"at": time.time(), "from": before, "to": rel["tag"], "mode": "release",
             "ok": True, "backup": str(backup)}
    _record(entry)
    _LAST_CHECK = None
    return {"ok": True, "mode": "release", "previous": before, "current": rel["tag"],
            "backup": str(backup), "needs_restart": True}


def rollback() -> dict[str, Any]:
    h = history()
    if not h:
        return {"ok": False, "error": "no update history"}
    entry = h[0]
    backup = entry.get("backup")
    if backup and Path(backup).exists():
        for item in Path(backup).iterdir():
            dest = ROOT / item.name
            if dest.is_dir():
                shutil.rmtree(dest, ignore_errors=True)
                shutil.copytree(item, dest)
            else:
                shutil.copy2(item, dest)
        return {"ok": True, "restored": backup}
    if is_git_checkout() and entry.get("from"):
        rc, out = _git("checkout", entry["from"])
        return {"ok": rc == 0, "to": entry["from"], "details": out}
    return {"ok": False, "error": "nothing to roll back to"}
