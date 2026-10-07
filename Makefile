.PHONY: gencode run reinstall clean lint test

gencode:
	python src/scripts/parse_code.py

run:
	./run.sh

reinstall:
	./run.sh --reinstall

clean:
	rm -rf .venv .pytest_cache .ruff_cache .mypy_cache
	find . -type d -name __pycache__ -exec rm -rf {} +

lint:
	ruff check src tests
	ruff format --check src tests

test:
	pytest -v
