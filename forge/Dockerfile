# The Forge: GPU training container (Unsloth handles the CUDA / PyTorch / TRL versions).
FROM unsloth/unsloth:latest

COPY forge/runner.py forge/train.py /opt/forge/

# Fail the build early, with a clear message, if Unsloth isn't importable from this Python.
# (find_spec doesn't import it, so this works without a GPU at build time.)
RUN python -c "import importlib.util, sys; ok = all(importlib.util.find_spec(m) for m in ('unsloth', 'trl', 'datasets')); \
sys.exit(0 if ok else 'The unsloth image changed: unsloth/trl/datasets are not importable from the default python')"

ENV FORGE_DIR=/forge PYTHONUNBUFFERED=1
WORKDIR /forge
ENTRYPOINT []
# The architect container (uid 1000) shares /forge, so hand it the folder and keep new files writable.
CMD ["sh", "-c", "chown -R 1000:1000 /forge && umask 000 && exec python /opt/forge/runner.py"]
