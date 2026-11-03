FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY pyproject.toml ./
COPY wetworks ./wetworks
COPY config ./config
RUN pip install --no-cache-dir .

RUN useradd -m architect && mkdir -p /data && chown architect /data
USER architect

CMD ["python", "-m", "wetworks.main"]
