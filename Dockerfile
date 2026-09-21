# NEMCast data API and daily ingestion — one image, two processes.
# docker-compose.yml runs it twice: once as the API, once as the scheduler.

FROM python:3.11-slim

# NEM time is AEST with no daylight saving, which is exactly Australia/Brisbane.
# The scheduler's 04:30 and 20:30, and daily.py's date window, both depend on it.
ENV TZ=Australia/Brisbane \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY api/requirements.txt api/requirements.txt
RUN pip install --no-cache-dir -r api/requirements.txt

COPY src/nemweb.py src/backfill.py src/daily.py src/
COPY api/app.py api/scheduler.py api/

# The warehouse is a mounted volume, not baked into the image, so data
# survives rebuilds and the image stays small.
RUN mkdir -p data/warehouse

EXPOSE 8000
CMD ["uvicorn", "api.app:app", "--host", "0.0.0.0", "--port", "8000"]
