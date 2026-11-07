.PHONY: install install-dev test format lint typecheck check-deps check live-check

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

# Installed versions vs. the requirements.txt pins (= the Pi's apt packages).
check-deps:
	python3 scripts/check_pinned_deps.py

check: test lint typecheck check-deps
	black --check product_monitor/ tests/ scripts/

live-check:
	python3 scripts/live_smoke_check.py
