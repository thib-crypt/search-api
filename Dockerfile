# crawl4ai ships a base image with Playwright + Chromium already installed,
# which saves a lengthy browser install step.
FROM unclecode/crawl4ai:latest

WORKDIR /srv/api

# uv for fast, reproducible installs.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install dependencies first for better layer caching.
COPY pyproject.toml README.md ./
RUN uv pip install --system --no-cache .

# App source.
COPY app ./app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health').status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
