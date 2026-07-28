#!/usr/bin/env bash
# One-command local demo: starts the mock reservation API, then the voice agent.
# Prereq: create .env with OPENAI_API_KEY, DEEPGRAM_API_KEY, CARTESIA_API_KEY.
# Then:   bash run_demo.sh   ->   open http://localhost:7860
set -e
cd "$(dirname "$0")"
PY=.venv/bin/python

if [ ! -f .env ]; then
  echo "!! No .env found. Run:  cp .env.example .env   then paste your 3 API keys into it."
  exit 1
fi

echo "Starting mock reservation API on :8000 ..."
$PY -m uvicorn app:app --app-dir mock_api --port 8000 --log-level warning &
API_PID=$!
trap 'echo; echo "Shutting down..."; kill $API_PID 2>/dev/null' EXIT INT TERM

for i in $(seq 1 40); do
  curl -sf http://localhost:8000/health >/dev/null 2>&1 && { echo "Mock API is up."; break; }
  sleep 0.5
done

echo "Starting voice agent on :7860 ..."
echo ">> Open http://localhost:7860 in Chrome, click Connect, allow the mic, and talk."
$PY -m luma_agent.bot
