FROM python:3.13-alpine

# Supervisor supplies these arguments for local builds. Do not rely on the
# retired builder to inject image metadata on our behalf.
ARG BUILD_VERSION=0.5.13
ARG BUILD_ARCH=amd64
LABEL io.hass.version="${BUILD_VERSION}" \
    io.hass.type="app" \
    io.hass.arch="${BUILD_ARCH}" \
    io.hass.name="Home Energy Manager" \
    io.hass.description="Dual-source load shedding, controlled recovery and configuration backup"

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data
COPY app.py core.py settings.py telemetry.py backup.py /app/
COPY web/index.html /app/web/index.html
COPY web/config.html /app/web/config.html
RUN python -c "from app import load_web_pages; load_web_pages(); print('Web assets verified')" \
    && mkdir -p /data
EXPOSE 1569
CMD ["python", "-u", "/app/app.py"]

