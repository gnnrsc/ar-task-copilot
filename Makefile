.PHONY: install playground run test

install:
	uv sync

playground:
	uv run adk web app --host 127.0.0.1 --port 18081 --reload_agents

run:
	uv run python -m app.fast_api_app

test:
	uv run pytest tests/unit
