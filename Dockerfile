FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    APP_HOST=0.0.0.0

WORKDIR /app

RUN useradd --system --uid 10001 --home /app appuser

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY run.py /app/run.py
COPY reseller_autoseller /app/reseller_autoseller

RUN mkdir -p /app/data && chown appuser:appuser /app/data

USER appuser
EXPOSE 8095
CMD ["python", "run.py"]
