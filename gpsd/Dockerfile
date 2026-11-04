FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends gpsd gpsd-clients python3-minimal \
    && rm -rf /var/lib/apt/lists/*

COPY source_registry.py gpsd_launcher.py /app/
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

CMD ["python3", "/app/gpsd_launcher.py", "--sources", "/etc/cartochron/sources.toml"]
