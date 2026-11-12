UV ?= uv
TF := $(or $(wildcard ../streamhouse/.tools/terraform),terraform)

.PHONY: install browsers test lint tf run faultbench bench azurite clean

install:  ## .venv with runtime + dev + airflow groups
	$(UV) sync --all-groups

browsers:  ## Chromium headless shell for the Playwright fetcher
	$(UV) run playwright install chromium --only-shell

test: lint  ## pytest: config, fetch, engine, monitor fault matrix, silver, dbt build, gate, DAG (+ Azurite if up)
	$(UV) run pytest

lint:
	$(UV) run ruff check .
	$(UV) run ruff format --check .

tf:  ## fmt + validate the Azure module (no credentials, no apply)
	$(TF) -chdir=terraform fmt -check -recursive
	$(TF) -chdir=terraform init -backend=false -input=false >/dev/null
	$(TF) -chdir=terraform validate

run:  ## scrape all configs -> monitor -> silver -> dbt build -> DQ gate (lake: $$SCRAPEWATCH_LAKE or ./lake)
	$(UV) run scrapewatch run

faultbench:  ## inject 13 faults + 4 clean scenarios into the local test site -> reports/faultbench.json
	$(UV) run scrapewatch faultbench

bench:  ## full pipeline twice against books/quotes.toscrape.com, fresh lake -> reports/bench.json
	$(UV) run scrapewatch bench

azurite:  ## Azurite blob emulator without Docker (or: docker compose up azurite)
	npx --yes azurite@3 azurite-blob --blobHost 127.0.0.1 --inMemoryPersistence --skipApiVersionCheck

clean:
	rm -rf lake lake-bench warehouse dbt/target dbt/logs logs .pytest_cache .ruff_cache
