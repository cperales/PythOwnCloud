FROM python:3.14-slim

# Minimal system deps
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg tini curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY pythowncloud/ ./pythowncloud/
COPY main.py .

# Create default data dir
RUN mkdir -p /data

# Run as non-root
RUN useradd -r -s /bin/false poc && chown -R poc:poc /app /data
USER poc

EXPOSE 8000

#HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
#  CMD curl -f http://localhost:8000/health || exit 1

# tini handles PID 1 + signal forwarding properly
ENTRYPOINT ["tini", "--"]
CMD ["uvicorn", "entrypoint:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
