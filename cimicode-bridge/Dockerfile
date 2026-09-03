FROM python:3.12-slim

WORKDIR /opt/bridge
COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
RUN pip install --no-cache-dir .

COPY scripts/bridge-entrypoint.sh /usr/local/bin/bridge-entrypoint.sh
RUN chmod +x /usr/local/bin/bridge-entrypoint.sh
EXPOSE 8081
ENTRYPOINT ["/usr/local/bin/bridge-entrypoint.sh"]
CMD ["run", "--host", "0.0.0.0", "--port", "8081"]