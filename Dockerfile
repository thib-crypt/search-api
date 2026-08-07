# crawl4ai ships a base image with Playwright + Chromium already installed,
# which saves a lengthy browser install step.
#
# Pinned by digest, not by tag: `:latest` silently changes the Chromium build and
# the crawl4ai version under an unchanged Dockerfile. This digest is the
# crawl4ai 0.8.9 multi-arch index — the same version uv.lock pins, so the image
# and the installed package cannot drift apart. To move: bump uv.lock first, then
# read the matching digest from
# `docker buildx imagetools inspect unclecode/crawl4ai:<tag>`.
FROM unclecode/crawl4ai@sha256:bd36741e7bdd35ddc1a05d9183e1d6d8cefb61dd640d944a25d026b76e917690

# The base image runs as the unprivileged `appuser`. Root is needed only to
# install system-wide; the final USER below drops back. The COPY --chmod flags
# also normalize file modes, which matters when the build context carries
# restrictive permissions (e.g. files checked out on a NAS with a tight umask).
USER root

WORKDIR /srv/api

# uv for fast, reproducible installs. Pinned by digest for the same reason.
COPY --from=ghcr.io/astral-sh/uv@sha256:069a51314a7bb6031777a9273205fe1b0b19e914ef418207d1338b268df641dd /uv /usr/local/bin/uv

# Dependencies come from uv.lock, not from the loose ranges in pyproject.toml, so
# a rebuild installs the exact versions that were tested. --locked fails the build
# if the lock has drifted from pyproject rather than silently re-resolving.
COPY --chmod=644 pyproject.toml uv.lock README.md ./
RUN uv export --locked --no-dev --no-emit-project --format requirements-txt -o /tmp/requirements.txt \
    && uv pip install --system --no-cache -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# The pinned playwright above may differ from the one the base image installed its
# browsers for, and playwright looks up Chromium under a version-stamped directory.
# Install the matching build into a shared, world-readable path so it resolves the
# same whether the process runs as root (build) or appuser (runtime).
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN playwright install chromium && chmod -R a+rX /ms-playwright

# App source. Installed with --no-deps: everything it needs is already pinned above.
COPY --chmod=755 app ./app
RUN uv pip install --system --no-cache --no-deps .

# Job database + cost ledger (§6.1). Created and chowned while still root so the
# path exists and is writable even when no volume is mounted over it; a bind
# mount from the host replaces it, which is what docker-compose does.
RUN mkdir -p /srv/api/data && chown -R appuser /srv/api/data

# Back to the base image's unprivileged user for runtime.
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health').status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
