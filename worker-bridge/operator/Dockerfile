FROM python:3.12-slim

RUN pip install --no-cache-dir kubernetes==33.1.0

COPY worker_bridge_operator.py /app/worker_bridge_operator.py

ENTRYPOINT ["python", "-u", "/app/worker_bridge_operator.py"]
