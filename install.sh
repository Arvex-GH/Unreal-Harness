#!/usr/bin/env bash
# Unreal Harness installer (Linux / macOS)
#   curl -fsSL https://raw.githubusercontent.com/Arvex-GH/Unreal-Harness/main/install.sh | bash
# or: ./install.sh --project /path/to/Game.uproject
set -euo pipefail

REPO="${HARNESS_REPO:-https://github.com/Arvex-GH/Unreal-Harness.git}"
INSTALL_DIR="${HARNESS_DIR:-$HOME/.local/share/unreal-harness}"
BIN_DIR="${HARNESS_BIN:-$HOME/.local/bin}"
PROJECT=""
NO_BRIDGE=0
BRANCH="${HARNESS_BRANCH:-main}"

c_info() { printf "\033[36m•\033[0m %s\n" "$*"; }
c_ok()   { printf "\033[32m✓\033[0m %s\n" "$*"; }
c_warn() { printf "\033[33m!\033[0m %s\n" "$*"; }
c_err()  { printf "\033[31m✗\033[0m %s\n" "$*" >&2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) PROJECT="${2:-}"; shift 2 ;;
    --dir) INSTALL_DIR="${2:-}"; shift 2 ;;
    --branch) BRANCH="${2:-}"; shift 2 ;;
    --no-bridge) NO_BRIDGE=1; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) c_err "unknown argument: $1"; exit 1 ;;
  esac
done

printf "\n\033[1m╭──────────────────────────────────────╮\n"
printf "│  \033[35mUnreal Harness\033[0m installer          │\n"
printf "╰──────────────────────────────────────╯\033[0m\n\n"

# --- python -----------------------------------------------------------------
PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1; then
    if "$c" -c 'import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)' 2>/dev/null; then
      PY="$c"; break
    fi
  fi
done
[[ -n "$PY" ]] || { c_err "Python 3.10+ is required. Install it and re-run."; exit 1; }
c_ok "python: $($PY --version)"

# --- fetch ------------------------------------------------------------------
if [[ -f "$(dirname "$0")/pyproject.toml" && ! -d "$INSTALL_DIR/.git" ]]; then
  SRC="$(cd "$(dirname "$0")" && pwd)"
  c_info "installing from local checkout: $SRC"
  mkdir -p "$INSTALL_DIR"
  if command -v rsync >/dev/null 2>&1; then
    rsync -a --exclude '.git' --exclude '.venv' --exclude '__pycache__' --exclude 'tests' "$SRC"/ "$INSTALL_DIR"/
  else
    # no rsync: use a tar pipe, which is portable and preserves modes
    ( cd "$SRC" && tar -cf - --exclude='.git' --exclude='.venv' --exclude='__pycache__' . ) \
      | ( cd "$INSTALL_DIR" && tar -xf - )
  fi
elif [[ -d "$INSTALL_DIR/.git" ]]; then
  c_info "updating existing install"
  git -C "$INSTALL_DIR" fetch --all --prune
  git -C "$INSTALL_DIR" pull --ff-only origin "$BRANCH" || git -C "$INSTALL_DIR" pull origin "$BRANCH"
else
  command -v git >/dev/null 2>&1 || { c_err "git is required"; exit 1; }
  c_info "cloning $REPO"
  git clone --branch "$BRANCH" "$REPO" "$INSTALL_DIR"
fi

# --- venv -------------------------------------------------------------------
VENV="$INSTALL_DIR/.venv"
if [[ ! -x "$VENV/bin/python" ]]; then
  c_info "creating virtualenv"
  "$PY" -m venv "$VENV"
fi
c_info "installing dependencies"
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet -e "$INSTALL_DIR"
c_ok "dependencies installed"

# --- launcher ---------------------------------------------------------------
mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/uh" <<LAUNCH
#!/usr/bin/env bash
exec "$VENV/bin/python" -m harness "\$@"
LAUNCH
chmod +x "$BIN_DIR/uh"
c_ok "launcher: $BIN_DIR/uh"

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) c_warn "add $BIN_DIR to your PATH:  export PATH=\"$BIN_DIR:\$PATH\"" ;;
esac

# --- engine bridge ----------------------------------------------------------
if [[ $NO_BRIDGE -eq 0 ]]; then
  if [[ -z "$PROJECT" ]]; then
    # try to find one near the cwd
    PROJECT="$(ls ./*.uproject 2>/dev/null | head -n1 || true)"
  fi
  if [[ -n "$PROJECT" && -f "$PROJECT" ]]; then
    c_info "installing engine bridge into $(dirname "$PROJECT")"
    "$VENV/bin/python" -m harness install-bridge --project "$PROJECT" || \
      c_warn "bridge install failed — you can retry later from Settings in the app"
  else
    c_warn "no .uproject found — install the bridge later from Settings"
  fi
fi

cat <<EOF

$(c_ok "Done.")

  Start:     uh
  Diagnose:  uh doctor
  Update:    uh update
  Data:      ~/.config/UnrealHarness  (or ~/Library/Application Support/UnrealHarness)
  Install:   $INSTALL_DIR

  1. Start LM Studio → Developer → Start Server
  2. Run \`uh\` — the UI opens at http://127.0.0.1:8787
  3. Settings → Install engine bridge → restart the editor

EOF
