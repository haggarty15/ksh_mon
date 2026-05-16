# Vanguard System Monitor — cloud container
FROM python:3.13-slim

WORKDIR /app

# Install Python dependencies
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Enable cloud mode by default (disables /api/trigger)
ENV CLOUD_MODE=1

# Data directory for the SQLite database (override with VANGUARD_DB_PATH)
ENV VANGUARD_DB_PATH=/app/data/vanguard_monitor.db

EXPOSE 8000

# Support the PORT env var used by Render, Railway, Fly, etc.
CMD ["sh", "-c", "python -m uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
