FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY wetworks ./wetworks
COPY config ./config
RUN pip install --no-cache-dir .

RUN useradd -u 1000 -m architect && mkdir -p /data/sandbox-runs /data/workspaces && chown -R architect /data
USER architect

CMD ["python", "-m", "wetworks.main"]
