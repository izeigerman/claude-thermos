VENV := .venv
BIN := $(VENV)/bin

.PHONY: clean install build publish lint format typecheck test style style-check

clean:
	rm -rf dist/

install:
	uv sync --all-packages --all-extras --inexact

build: install
	uv build

publish: clean build
	uv publish

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
