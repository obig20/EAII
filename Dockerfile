FROM python:3.12-slim

WORKDIR /app

ARG REQUIREMENT_FILE=requirements/prod.txt

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements/ ./requirements/
RUN pip install --no-cache-dir -r ${REQUIREMENT_FILE}

# Create unprivileged application user
RUN groupadd -g 10001 dailyclose && \
    useradd -u 10001 -g dailyclose -s /bin/false -m dailyclose

COPY . .

RUN chown -R dailyclose:dailyclose /app

USER dailyclose

ENV PYTHONPATH=/app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health').read()" || exit 1

