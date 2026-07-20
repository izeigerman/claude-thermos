VENV := .venv
BIN := $(VENV)/bin

.PHONY: install lint format fmt-check typecheck test check

install:
	uv sync --all-packages --inexact

lint:
	uv run ruff check . --fix

format:
	uv run ruff format .

typecheck:
	uv run ty check

test:
	uv run pytest -q

style: lint format typecheck
