FROM python:3.12-slim

RUN pip install --no-cache-dir kubernetes==33.1.0

COPY opencode_stack_operator.py /app/opencode_stack_operator.py

ENTRYPOINT ["python", "-u", "/app/opencode_stack_operator.py"]
