# Trading Alerts (self-hosted) in one container. Build context: the folder that holds engine/.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /src
COPY engine ./engine
RUN pip install "./engine[yahoo]" \
    && useradd --create-home --uid 1000 trader \
    && mkdir /work && chown trader:trader /work

WORKDIR /work
USER trader
EXPOSE 8321
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8321/api/session', timeout=4)"

ENTRYPOINT ["tradealerts"]
# Web interface + live checks. Needs TRADEALERTS_PASSWORD (see .env.example).
CMD ["serve", "--host", "0.0.0.0", "--port", "8321"]
