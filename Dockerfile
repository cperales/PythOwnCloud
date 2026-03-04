FROM python:3.14-trixie

# Minimal system deps
RUN apt-get update && \
    apt-get install -y --no-install-recommends tini && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY main.py config.py auth.py ./

# Create default data dir
RUN mkdir -p /data

# Run as non-root
RUN useradd -r -s /bin/false ocm && chown -R ocm:ocm /app
USER ocm

EXPOSE 8000

# tini handles PID 1 + signal forwarding properly
ENTRYPOINT ["tini", "--"]
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
