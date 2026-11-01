# syntax=docker/dockerfile:1
# Seedream image proxy: lets Open WebUI generate and edit images with BytePlus
# ModelArk Seedream. Built locally by `docker compose up --build`.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY seedream_proxy.py .
# Fails the build early if a dependency is missing or the code has a syntax error.
RUN python -c "import seedream_proxy"

# No reason to run as root.
RUN useradd --system --uid 10001 --no-create-home seedream
USER seedream

EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/health', timeout=4)"]

CMD ["python", "seedream_proxy.py"]
