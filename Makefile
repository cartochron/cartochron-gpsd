PYTHON ?= python3

check:
	$(PYTHON) -m unittest discover -s tests -p 'test_*.py' -v

# Check input channels before starting restart-managed containers.
check-config:
	GPSD_UDP_PORT_RANGE="$(or $(GPSD_UDP_PORT_RANGE),29998-30017)" $(PYTHON) gpsd_launcher.py --sources sources.toml --check

# Requires a local checkout of the independent Cartochron receiver.
check-integration:
	test -n "$(CARTOCHRON_DIR)" || { echo 'Set CARTOCHRON_DIR=/path/to/cartochron'; exit 1; }
	CARTOCHRON_DIR="$(CARTOCHRON_DIR)" $(PYTHON) -m unittest discover -s tests -p 'integration_check.py' -v

.PHONY: check check-integration check-config
