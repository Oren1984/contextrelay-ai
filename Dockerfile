FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    STREAMLIT_SERVER_FILE_WATCHER_TYPE=none \
    CONTEXTRELAY_DB_PATH=/data/contextrelay.db

WORKDIR /srv

COPY requirements.txt .
RUN pip install -r requirements.txt

RUN useradd --create-home --uid 10001 appuser \
    && mkdir /data && chown appuser:appuser /data

COPY --chown=appuser:appuser .streamlit .streamlit
COPY --chown=appuser:appuser app app
COPY --chown=appuser:appuser data/synthetic data/synthetic

USER appuser
EXPOSE 8501

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=3).status == 200 else 1)"

CMD ["streamlit", "run", "app/main.py", "--server.address=0.0.0.0", "--server.port=8501"]
