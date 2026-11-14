#!/usr/bin/env bash
# Uninstall Unreal Harness (Linux / macOS)
# Keeps your data unless --purge is passed.
set -uo pipefail
INSTALL_DIR="${HARNESS_DIR:-$HOME/.local/share/unreal-harness}"
DATA_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/UnrealHarness"
[[ "$(uname)" == "Darwin" ]] && DATA_DIR="$HOME/Library/Application Support/UnrealHarness"
PURGE=0
[[ "${1:-}" == "--purge" ]] && PURGE=1

echo "Removing launcher..."
rm -f "$HOME/.local/bin/uh"

echo "Removing app..."
rm -rf "$INSTALL_DIR"

echo "Removing engine bridge from projects you installed it into (if under ~/.local/share)..."
find "$HOME" -maxdepth 6 -type d -name UEHarness -path "*/Plugins/*" 2>/dev/null | while read -r d; do
  echo "  removing $d"
  rm -rf "$d"
done

if [[ $PURGE -eq 1 ]]; then
  echo "Removing data (chats, config, logs): $DATA_DIR"
  rm -rf "$DATA_DIR"
else
  echo "Kept your chats and settings in: $DATA_DIR (run with --purge to delete)"
fi
echo "Done."
