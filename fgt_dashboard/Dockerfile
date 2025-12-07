FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps
COPY fgt_dashboard/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code (not critical in dev if you bind-mount, but keeps prod usable)
COPY fgt_dashboard/ ./
COPY shared/ ./shared/

ENV PYTHONPATH=/app:/app/shared
EXPOSE 8050

# Start FastAPI (which mounts Dash at /admin)
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8050"]
