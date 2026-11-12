#!/usr/bin/env bash
set -o pipefail
# Run pytest quietly without warnings, short traceback, exit on first failure
OUTPUT=$(../.venv/bin/python -m pytest -q --tb=short --disable-warnings --maxfail=1 "$@" 2>&1)
STATUS=$?
if [ $STATUS -eq 0 ]; then
  # Succeeded: Return 3 words instead of hundreds of lines of tokens
  echo "✓ All tests passed"
  exit 0
else
  # Failed: Output ONLY the failure summary
  echo "✗ Tests failed:"
  echo "$OUTPUT" | grep -A 30 "FAILURES" || echo "$OUTPUT"
  exit $STATUS
fi
