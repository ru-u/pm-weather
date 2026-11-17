.DEFAULT_GOAL := help
.PHONY: help setup setup-all lint format typecheck test check discover log log-test report \
        settle-check nbm nbs p2a climo pull-data docker-build live-snow-inputs p2as-health

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-14s %s\n", $$1, $$2}'

setup:  ## Install the logger + research + dev environment (uv, Python 3.12)
	uv sync --extra research
	uv run pre-commit install

setup-all:  ## Also install the heavy weather / energy / modeling stacks
	uv sync --all-extras

lint:  ## Ruff lint + format check
	uv run ruff check src tests
	uv run ruff format --check src tests

format:  ## Auto-format and auto-fix
	uv run ruff format src tests
	uv run ruff check --fix src tests

typecheck:  ## mypy (strict on src/pmw)
	uv run mypy

test:  ## Unit tests (offline; fixtures are saved real responses)
	uv run pytest

check: lint typecheck test  ## Everything CI would run

discover:  ## List every open Kalshi market matching configs/series.yaml
	uv run pmw discover

log:  ## Run the market logger until stopped (writes to ./data unless PMW_S3_* is set)
	uv run pmw log

log-test:  ## Three quick logger cycles at a 60 s cadence, for a local smoke test
	uv run pmw log --cycles 3 --base-s 60 --hot-s 20 --no-align

report:  ## Daily logger report for yesterday (UTC); `make report DATE=2026-11-14`
	uv run pmw report $(if $(DATE),--date $(DATE),)

settle-check:  ## Replicate settled snow markets; `make settle-check SERIES=all`
	uv run pmw settle-check $(or $(SERIES),KXNYCSNOWM) --brief

nbm:  ## Pull and parse NBM probabilistic bulletins for the five sample winters (resumable)
	for w in "2021-11-01 2022-03-31" "2022-11-01 2023-03-31" "2023-11-01 2024-03-31" "2024-11-01 2025-03-31" "2025-11-01 2026-03-31"; do \
		set -- $$w; uv run pmw nbm-pull $$1 $$2; done
	uv run pmw nbm-parse 2021-11-01 2026-03-31

nbs:  ## Pull and parse NBM short-range bulletins (6-hour snowfall) for the five sample winters
	for w in "2021-11-01 2022-03-31" "2022-11-01 2023-03-31" "2023-11-01 2024-03-31" "2024-11-01 2025-03-31" "2025-11-01 2026-03-31"; do \
		set -- $$w; uv run pmw nbm-pull $$1 $$2 --product nbs; done
	uv run pmw nbs-parse 2021-11-01 2026-03-31

p2a:  ## Run experiment P2A end to end (needs make nbm nbs climo first)
	uv run pmw obs-build
	uv run pmw p2a-forecasts
	uv run pmw p2a-score

LIVE_SNOW ?= data/interim/live_snow
LIVE_END ?= $(shell date -u +%F)

live-snow-inputs:  ## Winter 2026-27 composite inputs into $(LIVE_SNOW) (no prices); `LIVE_END=2027-04-30`
	mkdir -p $(LIVE_SNOW)/obs $(LIVE_SNOW)/nbm $(LIVE_SNOW)/climo
	uv run pmw nbm-pull 2026-11-01 $(LIVE_END)
	uv run pmw nbm-parse 2026-11-01 $(LIVE_END) --out $(LIVE_SNOW)/nbm/nbp_periods.parquet
	uv run pmw nbm-pull 2026-11-01 $(LIVE_END) --product nbs
	uv run pmw nbs-parse 2026-11-01 $(LIVE_END) --out $(LIVE_SNOW)/nbm/nbs_s06.parquet
	uv run pmw climo-pull --end $(LIVE_END) --out $(LIVE_SNOW)/climo/daily_snow.parquet
	uv run pmw obs-build --all-stations --first-year 2026 --last-year 2027 \
		--climo $(LIVE_SNOW)/climo/daily_snow.parquet --out $(LIVE_SNOW)/obs/daily_snow.parquet

p2as-health:  ## P2A-S data health (counts only); pull live/kalshi/market_meta and orderbooks first
	uv run pmw p2as-health

climo:  ## Pull 35 years of daily snowfall per station for climatology
	uv run pmw climo-pull

pull-data:  ## Copy the Railway bucket to ./data/bucket (needs PMW_S3_* in .env)
	uv run --env-file .env python scripts/pull_data.py $(PREFIX)

docker-build:  ## Build the lean logger image locally
	docker build -t pmw-logger .
