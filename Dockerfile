FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    FASTEMBED_CACHE_DIR=/opt/fastembed \
    DISPLAY=:99

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir . \
    && python -c "from fastembed import TextEmbedding; TextEmbedding('sentence-transformers/all-MiniLM-L6-v2', cache_dir='/opt/fastembed'); print('fastembed model pre-warmed')"

# Headful chromium (Xvfb) + fonts for the native browser/stealth tiers.
RUN apt-get update && apt-get install -y --no-install-recommends xvfb fontconfig fonts-liberation fonts-noto-color-emoji && rm -rf /var/lib/apt/lists/* \
    && fc-cache -f
RUN python -m patchright install chromium \
    && python -m patchright install-deps chromium

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8780
HEALTHCHECK --interval=30s \
            --timeout=10s \
            --start-period=60s \
            --retries=5 \
CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8780/health', timeout=5).status==200 else 1)"

ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "wellisearch.app:app", "--host", "0.0.0.0", "--port", "8780"]
