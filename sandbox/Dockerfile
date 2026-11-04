# Test sandbox: the project's dependencies and pytest, no secrets, no network at run time.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /sandbox

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

# Install Wetworks' own dependencies so its tests can run offline.
COPY pyproject.toml /tmp/project/pyproject.toml
RUN python -c "import tomllib;d=tomllib.load(open('/tmp/project/pyproject.toml','rb'));print('\n'.join(d['project']['dependencies']+d['project'].get('optional-dependencies',{}).get('dev',[])))" > /tmp/reqs.txt \
 && pip install --no-cache-dir -r /tmp/reqs.txt pytest \
 && rm -rf /tmp/project /tmp/reqs.txt

COPY sandbox/server.py /sandbox/server.py
RUN useradd -u 1000 -m runner && mkdir -p /runs && chown runner /runs
USER runner

CMD ["python", "/sandbox/server.py"]
