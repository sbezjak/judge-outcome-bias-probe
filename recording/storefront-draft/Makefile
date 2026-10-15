.PHONY: test test-unit

VENV_PY = .venv/bin/python

# the full suite: module tests plus the e2e order replay (about 4 minutes)
test:
	$(VENV_PY) -m pytest tests/ -q

# module tests only (seconds) - for quick checks while you work
test-unit:
	$(VENV_PY) -m pytest tests/ -q --ignore=tests/e2e
