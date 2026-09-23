# syntax=docker/dockerfile:1.6
# Corpus-runner image: stdlib-only asyncio driver (see harness.py).
FROM python:3.12-alpine
COPY harness.py /usr/local/bin/harness.py
ENTRYPOINT ["python3", "/usr/local/bin/harness.py"]
