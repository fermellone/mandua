.PHONY: sync test check build demo tutorial-check conformance-check
sync:
	uv sync --locked
test:
	uv run pytest -q
check:
	uv run ruff format --check .
	uv run ruff check .
	uv run pytest -q
build:
	uv build
demo:
	uv run mandua demo
tutorial-check:
	uv run python scripts/render_tutorial.py --check
	uv run pytest tests/acceptance/test_tutorial.py -q
conformance-check:
	uv run pytest tests/unit/test_conformance_manifest.py tests/acceptance/test_conformance.py tests/acceptance/test_security_boundaries.py -q
