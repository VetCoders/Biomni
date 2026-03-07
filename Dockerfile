FROM python:3.12-slim AS builder

ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
  && apt-get install -y --no-install-recommends build-essential gcc \
  && rm -rf /var/lib/apt/lists/*

RUN python -m venv "$VIRTUAL_ENV"

WORKDIR /build
COPY requirements.txt ./
# Copy biomni package for local runtime imports
COPY biomni/ ./biomni/

RUN pip install --upgrade pip setuptools wheel \
  && pip install -r requirements.txt

FROM python:3.12-slim AS runtime

ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

RUN groupadd --system app \
  && useradd --system --gid app --create-home --home-dir /home/app --shell /usr/sbin/nologin app

COPY --from=builder /opt/venv /opt/venv
COPY app/ ./app/
COPY static/ ./static/
COPY biomni/ ./biomni/
COPY start.sh ./start.sh
COPY requirements.txt ./requirements.txt

RUN mkdir -p /app/data \
  && chmod +x /app/start.sh \
  && chown -R app:app /app /home/app

EXPOSE 8129
USER app

CMD ["./start.sh"]
