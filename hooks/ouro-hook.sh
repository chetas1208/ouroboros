#!/bin/bash
# Cursor hook entrypoint for Ouroboros. Usage: ouro-hook.sh prompt|stop|shell
# Reads the hook JSON on stdin, writes the hook JSON response on stdout.
# Fails open: if ouro is missing or errors, return a neutral response so Cursor is never blocked by tooling trouble.
OURO="${OURO_BIN:-$HOME/.ouroboros/bin/ouro}"
case "$1" in
  prompt) neutral='{"continue": true}' ;;
  stop)   neutral='{}' ;;
  shell)  neutral='{"permission": "allow"}' ;;
  *)      echo '{}'; exit 0 ;;
esac
if [ ! -x "$OURO" ]; then echo "$neutral"; exit 0; fi
input=$(cat)
out=$(printf '%s' "$input" | "$OURO" "hook-$1" 2>/dev/null)
if [ -z "$out" ]; then echo "$neutral"; else echo "$out"; fi
exit 0
