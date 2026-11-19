FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    GROCERIES_DATA_DIR=/data GROCERIES_SECRETS_DIR=/data/secrets \
    GROCERIES_HOST=0.0.0.0 GROCERIES_PORT=8000

WORKDIR /app
COPY requirements.lock ./
# git is only needed for the optional public price import (GROCERIES_PUBLIC_PRICES)
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
RUN pip install -r requirements.lock
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-deps . && useradd --system --uid 10001 --home-dir /data groceries \
    && mkdir -p /data && chown groceries:groceries /data

USER groceries
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=1m --timeout=5s CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('GROCERIES_PORT', '8000'))"
ENTRYPOINT ["groceries"]
CMD ["serve"]
