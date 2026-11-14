#!/usr/bin/env python3
"""Build distributable archives (zip + tar.gz) and checksums."""
import hashlib, shutil, subprocess, sys, tarfile, zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "releases"
VERSION = (ROOT / "VERSION").read_text().strip()
SKIP = {".git", ".venv", "__pycache__", "dist", "releases", "build", "*.egg-info", ".pytest_cache"}


def files():
    for p in ROOT.rglob("*"):
        if any(part in SKIP for part in p.parts):
            continue
        if p.is_file() and p.name not in (".DS_Store",):
            yield p


def main() -> int:
    DIST.mkdir(exist_ok=True)
    stem = f"unreal-harness-{VERSION}"
    zpath = DIST / f"{stem}.zip"
    tpath = DIST / f"{stem}.tar.gz"
    count = 0
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files():
            z.write(f, f"{stem}/{f.relative_to(ROOT)}")
            count += 1
    with tarfile.open(tpath, "w:gz") as t:
        for f in files():
            t.add(f, f"{stem}/{f.relative_to(ROOT)}")
    lines = []
    for p in (zpath, tpath):
        h = hashlib.sha256(p.read_bytes()).hexdigest()
        lines.append(f"{h}  {p.name}")
        print(f"{p.name}  {p.stat().st_size//1024} KB")
    (DIST / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n")
    print(f"\n{count} files  ->  {DIST}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
