.PHONY: seed backtest forward settle quality export nightly test uismoke audit serve clean

PY ?= python3

seed:
	$(PY) scripts/seed.py

backtest:
	$(PY) -m parlaysports.run backtest

forward:
	$(PY) -m parlaysports.run forward --days 7

settle:
	$(PY) -m parlaysports.run settle

quality:
	$(PY) -m parlaysports.run quality

export:
	$(PY) -m parlaysports.run export

nightly:
	$(PY) scripts/nightly.py

test:
	$(PY) -m unittest discover -s tests -q

uismoke:
	node scripts/ui_smoke.mjs

audit:
	$(PY) scripts/audit.py

serve:
	$(PY) -m http.server 8000 --bind 0.0.0.0

clean:
	rm -rf data/parlaysports.db* data/site/*.json data/audit_report.json
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
