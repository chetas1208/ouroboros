#!/usr/bin/env bash
# Install Ouroboros for Cursor. Idempotent. Never overwrites an existing ~/.cursor/hooks.json or rule you edited.
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
HOME_DIR="${OURO_HOME:-$HOME/.ouroboros}"
CURSOR="${CURSOR_HOME:-$HOME/.cursor}"

command -v python3 >/dev/null || { echo "python3 (3.9+) is required"; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' || { echo "python 3.9+ is required"; exit 1; }

mkdir -p "$HOME_DIR/bin" "$HOME_DIR/skill/scripts" "$CURSOR/hooks" "$CURSOR/rules" "$CURSOR/skills"
cp "$SRC/bin/ouro" "$HOME_DIR/bin/ouro"
cp "$SRC/skill/SKILL.md" "$HOME_DIR/skill/SKILL.md"
cp "$SRC/skill/scripts/ouro.py" "$HOME_DIR/skill/scripts/ouro.py"
cp "$SRC/hooks/ouro-hook.sh" "$CURSOR/hooks/ouro-hook.sh"
chmod +x "$HOME_DIR/bin/ouro" "$CURSOR/hooks/ouro-hook.sh"
echo "installed CLI        -> $HOME_DIR/bin/ouro"

# Skill: symlink so updates to $HOME_DIR/skill are picked up.
[ -e "$CURSOR/skills/ouroboros" ] || ln -s "$HOME_DIR/skill" "$CURSOR/skills/ouroboros"
echo "installed skill      -> $CURSOR/skills/ouroboros"

# Rule: only if absent.
if [ -e "$CURSOR/rules/ouroboros.mdc" ]; then echo "rule exists, left alone: $CURSOR/rules/ouroboros.mdc"
else cp "$SRC/rules/ouroboros.mdc" "$CURSOR/rules/ouroboros.mdc"; echo "installed rule       -> $CURSOR/rules/ouroboros.mdc"; fi

# Hooks: only if absent; otherwise print what to merge.
if [ -e "$CURSOR/hooks.json" ]; then
  echo "hooks.json exists, NOT modified. Merge the entries from: $SRC/hooks/hooks.json"
else cp "$SRC/hooks/hooks.json" "$CURSOR/hooks.json"; echo "installed hooks      -> $CURSOR/hooks.json"; fi

echo; echo "Add to your shell profile:  export PATH=\"$HOME_DIR/bin:\$PATH\""
echo "Then run: ouro doctor   (and: ollama pull qwen2.5:3b)"
