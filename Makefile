VENV := .venv
BIN := $(VENV)/bin

.PHONY: install lint format fmt-check typecheck test check style style-check

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

style-check: style
	@if [ -n "$$(git status --porcelain)" ]; then \
		echo "make style modified files. Run 'make style' locally and commit the changes."; \
		git status --porcelain; \
		git diff; \
		exit 1; \
	fi
