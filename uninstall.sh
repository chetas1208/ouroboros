#!/usr/bin/env bash
# Remove what install.sh added. Leaves hooks.json alone (edit it by hand if you merged entries).
set -euo pipefail
HOME_DIR="${OURO_HOME:-$HOME/.ouroboros}"; CURSOR="${CURSOR_HOME:-$HOME/.cursor}"
rm -f "$CURSOR/hooks/ouro-hook.sh" "$CURSOR/rules/ouroboros.mdc"
[ -L "$CURSOR/skills/ouroboros" ] && rm "$CURSOR/skills/ouroboros"
rm -rf "$HOME_DIR"
echo "removed. If you installed hooks.json from this repo, delete the ouro entries in $CURSOR/hooks.json"
