#!/bin/bash
# Pokreni pytest smoke suite.
# Spawn-uje sopstvenu relay instancu na slobodnom portu (ne kontaminira dev).

set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Kill bilo koji vec aktivan relay da ne kolizi sa test-om
# (tests spawn-uju svoj na free port-u, ali da budemo sigurni)
pkill -f "uvicorn.*relay" 2>/dev/null || true

exec .venv/bin/python -m pytest tests/ "$@"
