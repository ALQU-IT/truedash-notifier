FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ .

RUN useradd --system --no-create-home notifier \
    && mkdir -p /data \
    && chown notifier:notifier /data
USER notifier

VOLUME ["/data"]

EXPOSE 7842

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:7842/health')"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7842"]
