PYTHON ?= .venv/bin/python

.PHONY: install check preview release-plan

install:
	python3 -m venv .venv
	$(PYTHON) -m pip install -r requirements.txt

check:
	$(PYTHON) -m unittest discover -s tests -v
	$(PYTHON) -m postpub check

preview:
	$(PYTHON) -m postpub build --target preview --output build/preview

release-plan:
	$(PYTHON) -m postpub build --target release --output build/release
