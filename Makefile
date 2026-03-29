.PHONY: build dev lint test clean install

# ── Image build ───────────────────────────────────────────────────────────────
build:
	@echo "Building Pi image (requires Docker)..."
	cd image && bash build.sh
	@echo "Image ready in image/dist/"

# ── Local development ─────────────────────────────────────────────────────────
dev:
	@echo "Starting gateway in dev mode (no Pi hardware required)..."
	@test -f opencpo.yaml || (cp config/opencpo.yaml.example opencpo.yaml && \
		echo "Created opencpo.yaml — edit it before running")
	OPENCPO_TAILSCALE_AUTH_KEY=dev \
	OPENCPO_CORE_API_URL=http://localhost:8000 \
	python -m gateway.main

# ── Install deps ──────────────────────────────────────────────────────────────
install:
	pip install -r requirements.txt

# ── Linting ───────────────────────────────────────────────────────────────────
lint:
	@echo "Running ruff..."
	ruff check gateway/
	@echo "Running mypy..."
	mypy gateway/ --ignore-missing-imports --no-strict-optional
	@echo "Lint passed ✓"

# ── Tests ─────────────────────────────────────────────────────────────────────
test:
	@echo "Running tests..."
	pytest tests/ -v --tb=short

# ── Clean ─────────────────────────────────────────────────────────────────────
clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	rm -rf .mypy_cache .ruff_cache .pytest_cache
	rm -rf image/dist/

# ── Helper: flash (macOS/Linux) ───────────────────────────────────────────────
flash:
	@echo "Usage: make flash DEVICE=/dev/sdX"
	@test -n "$(DEVICE)" || (echo "Set DEVICE=... e.g. make flash DEVICE=/dev/sdb" && exit 1)
	@ls image/dist/*.img.gz 2>/dev/null | head -1 | xargs -I{} sh -c \
		'echo "Flashing {}..." && gunzip -c {} | sudo dd of=$(DEVICE) bs=4M status=progress && sync'
