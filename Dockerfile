# Minimal image running the VXI proxy facade (if available) and the config GUI
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src

WORKDIR /app

# Install runtime dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code and default config
COPY src ./src
COPY scripts ./scripts

# Expose GUI and default facade port; adjust as needed via config.yaml
EXPOSE 8080 1024

# Default environment; can be overridden at docker run
ENV CONFIG_PATH=/app/config.yaml \
    GUI_HOST=0.0.0.0 \
    GUI_PORT=8080 \
    SERVER_HOST_OVERRIDE=0.0.0.0

ENTRYPOINT ["python", "scripts/docker_entrypoint.py"]
