VENV := .venv
BIN := $(VENV)/bin

.PHONY: install lint format fmt-check typecheck test check

install:
	uv sync --all-packages --inexact

lint:
	uv run ruff check .

format:
	uv run ruff format .

fmt-check:
	uv run ruff format --check .

typecheck:
	uv run ty check

test:
	uv run pytest -q

check: lint fmt-check typecheck test
