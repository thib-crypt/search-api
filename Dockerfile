# crawl4ai ships a base image with Playwright + Chromium already installed,
# which saves a lengthy browser install step.
FROM unclecode/crawl4ai:latest

# crawl4ai's base image runs as a non-root user. Switch to root so we can
# install system-wide; the COPY --chmod below also normalizes file modes,
# which matters when the build context carries restrictive permissions
# (e.g. files checked out on a NAS with a tight umask).
USER root

WORKDIR /srv/api

# uv for fast, reproducible installs.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install dependencies first for better layer caching.
COPY --chmod=644 pyproject.toml README.md ./
RUN uv pip install --system --no-cache .

# App source.
COPY --chmod=755 app ./app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health').status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
