PYTHON ?= python3

check:
	$(PYTHON) -m unittest discover -s tests -p 'test_*.py' -v

# Requires a local checkout of the independent Cartochron receiver.
check-integration:
	test -n "$(CARTOCHRON_DIR)" || { echo 'Set CARTOCHRON_DIR=/path/to/cartochron'; exit 1; }
	CARTOCHRON_DIR="$(CARTOCHRON_DIR)" $(PYTHON) -m unittest discover -s tests -p 'integration_check.py' -v

.PHONY: check check-integration
