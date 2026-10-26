.PHONY: test test-gateway test-woo test-ref test-mcpkit ui lint-chart dev up down
PY ?= python3

test: test-mcpkit test-woo test-ref test-gateway lint-chart   ## all unit + end-to-end tests (no cluster needed)

test-mcpkit:
	cd libs/mcpkit && PYTHONPATH=. $(PY) -m unittest discover -s tests
test-woo:
	cd services/woo-mcp && PYTHONPATH=.:../../libs/mcpkit $(PY) -m unittest discover -s tests
test-ref:
	cd services/ref-mcp && PYTHONPATH=.:../../libs/mcpkit $(PY) -m unittest discover -s tests
test-gateway:
	cd services/gateway && PYTHONPATH=.:../../libs/mcpkit $(PY) -m unittest discover -s tests -t .

lint-chart:                                                   ## structural lint; use `helm lint` for the real thing
	$(PY) scripts/lint_chart.py

ui:                                                           ## build the dashboard bundle locally
	cd services/dashboard && npm install && npm run build

dev: ui                                                       ## whole platform on localhost, no Kubernetes
	$(PY) scripts/dev_stack.py

up:                                                           ## kind + ingress + images + helm (see README)
	scripts/local-up.sh
down:
	scripts/local-down.sh
