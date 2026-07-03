.PHONY: install install-dev test format lint typecheck check live-check

install:
	pip install -r requirements.txt

install-dev:
	pip install -r requirements-dev.txt

test:
	python3 -m pytest tests/ -v

format:
	black product_monitor/ tests/ scripts/

lint:
	ruff check product_monitor/ tests/ scripts/

typecheck:
	mypy product_monitor/

check: test lint typecheck
	black --check product_monitor/ tests/ scripts/

live-check:
	python3 scripts/live_smoke_check.py
