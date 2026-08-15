# AsyncGate — the execution boundary.
#
# Build context is the STACK ROOT, not this repository:
#
#     docker build -f AsyncGate/Dockerfile .
#
# The image must install the canonical protocol package from the sibling
# LegiVellum checkout. Previously it did not, and the receipt adapter resolved
# `legivellum` by walking parent directories for a source tree that does not
# exist in an image. That import failed, the failure was swallowed by
# `except ImportError`, and AsyncGate POSTed unvalidated payloads to the ledger
# in every deployment including the demo stack.

FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# The canonical protocol package first: receipt models, validation, and the
# schema, which ships as package data so validation needs no source checkout.
COPY LegiVellum/pyproject.toml LegiVellum/README.md /src/LegiVellum/
COPY LegiVellum/shared/ /src/LegiVellum/shared/
RUN pip install --no-cache-dir /src/LegiVellum

# Copy project files
COPY AsyncGate/pyproject.toml .
COPY AsyncGate/README.md .
COPY AsyncGate/src/ src/
COPY AsyncGate/alembic.ini .
COPY AsyncGate/alembic/ alembic/

# Install Python dependencies
RUN pip install --no-cache-dir .

# Fail the build if the receipt validator is not importable. An image that
# cannot validate must not be publishable, rather than degrading at runtime.
RUN python -c "import legivellum.validation as v; p = v.schema_path(); assert p.exists(), p; print('receipt schema resolved at', p)"

# Create non-root user
RUN addgroup --system --gid 1001 asyncgate && \
    adduser --system --uid 1001 --gid 1001 asyncgate && \
    chown -R asyncgate:asyncgate /app

USER asyncgate

# Expose ports
EXPOSE 8080
EXPOSE 9091

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import json, os, urllib.request; url=os.environ.get('ASYNCGATE_MCP_URL','http://localhost:8080/mcp'); token=os.environ.get('ASYNCGATE_API_KEY') or os.environ.get('ASYNCGATE_AUTH_TOKEN'); payload={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'asyncgate.health','arguments':{}}}; req=urllib.request.Request(url, data=json.dumps(payload).encode(), headers={'Content-Type':'application/json'}); token and req.add_header('Authorization','Bearer '+token); resp=urllib.request.urlopen(req, timeout=5); data=json.load(resp); assert 'result' in data"

# Run the application
CMD ["uvicorn", "asyncgate.main:app", "--host", "0.0.0.0", "--port", "8080"]
