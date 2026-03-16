.PHONY: test test-all test-01 test-02 test-03 test-04 test-05 test-06 test-verbose test-coverage clean

test: test-all

test-all:
	python3 -m pytest -q

test-01:
	python3 -m pytest challenge-01/ -v

test-02:
	python3 -m pytest challenge-02/ -v

test-03:
	python3 -m pytest challenge-03/ -v

test-04:
	python3 -m pytest challenge-04/ -v

test-05:
	python3 -m pytest challenge-05/ -v

test-06:
	python3 -m pytest challenge-06/ -v

test-verbose:
	python3 -m pytest -v

test-coverage:
	python3 -m pytest --cov=challenge-01 --cov=challenge-02 --cov=challenge-03 --cov=challenge-04 --cov=challenge-05 --cov=challenge-06 --cov-report=term-missing

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .python3 -m pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -name '*.pyc' -delete 2>/dev/null || true
