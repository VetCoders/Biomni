FROM python:3.12-slim AS builder
RUN apt-get update \
  && apt-get install -y --no-install-recommends build-essential \
  && rm -rf /var/lib/apt/lists/*
RUN python -m venv "/opt/venv"
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --upgrade pip
WORKDIR /build
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

FROM python:3.12-slim AS runtime
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1
WORKDIR /app
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser
COPY --from=builder /opt/venv /opt/venv
COPY app ./app
COPY static ./static
COPY requirements.txt ./requirements.txt
EXPOSE 8129
USER appuser
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8129"]
