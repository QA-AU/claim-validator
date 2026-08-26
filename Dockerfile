# Build and push with the platform pinned explicitly:
#
#   docker buildx build --platform linux/amd64 \
#     -t <registry>.azurecr.io/claim-validator:latest --push .
#
# Azure Container Apps requires linux/amd64. A bare `docker build` (no
# --platform) defaults to the host's own architecture — on an Apple
# Silicon Mac that's linux/arm64, and the build and push both "succeed"
# regardless. The mismatch only surfaces later, at pull time in Azure,
# and — found live, cost a long investigation — as a misleading
# "unauthorized" pull error, not an obvious platform error: see
# infra/README.md's "Resolved: usera tenant's image pull failure".
#
# Runs the app as a plain source checkout, not an installed wheel — the
# same way it has been run and tested throughout this project (README's
# own instructions are `uvicorn claimvalidator.api:app` from the repo
# root). Deliberate: pyproject.toml's wheel target only packages
# phases/db/claimvalidator (see [tool.hatch.build.targets.wheel]), which
# would silently drop phase1_model_config.py — a real top-level module
# phases/cli_client.py imports directly. `pip install .` below is used
# only to pull in third-party dependencies via pyproject.toml as the
# single source of truth; the source copy that follows is what Python
# actually imports at runtime (the working directory resolves before
# site-packages), so that packaging gap never bites here.
FROM python:3.12-slim

WORKDIR /app

# Dependencies in their own layer, cached unless pyproject.toml changes.
COPY pyproject.toml ./
RUN pip install --no-cache-dir .

# The actual source, copied in after — see the note above for why this,
# not the pip-installed copy, is what runs.
COPY phase1_model_config.py ./
COPY phases/ ./phases/
COPY db/ ./db/
COPY claimvalidator/ ./claimvalidator/

RUN useradd --create-home --uid 1000 appuser && chown -R appuser:appuser /app
USER appuser

ENV PORT=8000
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request, os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", 8000)}/api/ping', timeout=3)" || exit 1

# Shell form so $PORT expands — Container Apps sets its own target port
# separately in ingress config, but honouring $PORT here still lets this
# image run correctly on any platform that does inject it.
CMD uvicorn claimvalidator.api:app --host 0.0.0.0 --port $PORT
