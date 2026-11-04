FROM python:3.11-slim-bookworm

WORKDIR /app
COPY cartochron_gpsd.py source_registry.py /app/
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

CMD ["python3", "/app/cartochron_gpsd.py"]
