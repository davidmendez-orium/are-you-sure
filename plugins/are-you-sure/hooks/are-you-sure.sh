#!/bin/sh
# Interpreter shim. Claude Code runs hooks on the host; Cowork runs them inside a
# VM, and neither documents what is on its PATH. Resolve an interpreter, and exit
# 0 quietly if there is none — a checker that cannot run must not print an error
# on every turn, and must never look like a reason to stop the session.
#
# Run `sh are-you-sure.sh --selftest` in any environment to see which of these it
# found and whether the checker works there.

DIR=$(dirname "$0")

for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then
    exec "$candidate" "$DIR/are_you_sure.py" "$@"
  fi
done

if [ "$1" = "--selftest" ]; then
  echo "are-you-sure: FAIL — no python3 or python on PATH; the hook is inert here." >&2
  exit 1
fi

exit 0
