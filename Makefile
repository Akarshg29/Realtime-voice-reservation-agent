.PHONY: install api api-local agent test eval eval-llm reset clean

VENV=.venv
PY=$(VENV)/bin/python
PIP=$(VENV)/bin/pip

install:
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[dev]"

# Run the provided reservation API (Docker).
api:
	docker compose up --build

# Run the reservation API natively (no Docker needed).
api-local:
	$(PY) -m uvicorn app:app --app-dir mock_api --host 0.0.0.0 --port 8000

# Run the voice agent server (browser client at http://localhost:7860).
agent:
	$(PY) -m luma_agent.bot

# Fast, no-keys tool + scenario tests against the mock API.
test:
	$(PY) -m pytest -q

# Scripted evaluation (no keys) -> EVALUATION_RESULTS.md
eval:
	$(PY) eval/run_eval.py --mode scripted

# Full LLM-driven evaluation (needs OPENAI_API_KEY) -> EVALUATION_RESULTS.md
eval-llm:
	$(PY) eval/run_eval.py --mode llm --api-url http://localhost:8000

# Reset the mock API's data.
reset:
	curl -s -X POST http://localhost:8000/admin/reset && echo

clean:
	rm -rf $(VENV) .pytest_cache **/__pycache__
