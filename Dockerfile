FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ .
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN useradd --system --no-create-home --shell /sbin/nologin notifier \
    && chmod +x /usr/local/bin/docker-entrypoint.sh \
    && mkdir -p /data \
    && chown notifier:notifier /data
USER notifier

VOLUME ["/data"]

EXPOSE 7842

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request, ssl; ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE; urllib.request.urlopen('https://localhost:7842/health', context=ctx)"

CMD ["/usr/local/bin/docker-entrypoint.sh"]
