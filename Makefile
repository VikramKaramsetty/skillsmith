PY ?= 3.12
UV := uv run --python $(PY) --extra dev --extra dashboard
UV_HEAVY := uv run --python $(PY) --extra dev --extra dashboard --extra sim --extra kin --extra vision --extra droid

.PHONY: droid-sandbox-check setup check check-heavy bench test lint fmt demo-fake validate-library status

setup:
	uv sync --python $(PY) --extra dev --extra dashboard

check: lint test validate-library

# Same tests with mujoco/placo/opencv installed, so WS2/WS3 tests run instead of skipping.
check-heavy:
	SKILLSMITH_HEAVY=1 $(UV_HEAVY) pytest -q

test:
	$(UV) pytest -q

# Seeded-bug benchmark (WS5.6): fix rate and seconds-to-fix per class. ARGS="--author droid" for live Droid.
bench:
	$(UV) python -m skillsmith.repair.benchmark $(ARGS)

lint:
	$(UV) ruff check src tests scripts robotd assets/so101/fetch_so101.py

fmt:
	$(UV) ruff check --fix src tests scripts robotd assets/so101/fetch_so101.py
	$(UV) ruff format src tests scripts robotd assets/so101/fetch_so101.py

validate-library:
	@for f in skills/library/*.py; do $(UV) skillsmith validate $$f || exit 1; done

demo-fake:
	$(UV) skillsmith demo-fake

status:
	@scripts/status.sh


# Live proof that Droid's tool sandbox holds (needs FACTORY_API_KEY + credits).
droid-sandbox-check:
	uv run --python $(PY) --extra dev --extra dashboard --extra droid python tools/droid_sandbox_check.py
