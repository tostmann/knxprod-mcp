# knxprod-mcp: MCP server for KNX manufacturer product files (.knxprod)
FROM python:3.13.18-slim-trixie

LABEL org.opencontainers.image.title="knxprod-mcp" \
      org.opencontainers.image.description="MCP server for KNX manufacturer product files (.knxprod)" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --system --gid 10001 knxprod \
 && useradd --system --uid 10001 --gid 10001 --home-dir /nonexistent --no-create-home \
            --shell /usr/sbin/nologin knxprod \
 && mkdir -p /knxprod /data \
 && chown 10001:10001 /knxprod /data

COPY knxprod_mcp.py LICENSE /app/

USER 10001:10001
VOLUME ["/knxprod", "/data"]
EXPOSE 8095

# The first start indexes the whole product folder before the server listens.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5m --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8095/healthz', timeout=4).status == 200 else 1)"]

ENTRYPOINT ["python", "/app/knxprod_mcp.py"]
CMD ["serve", "--bind", "0.0.0.0", "--port", "8095", "--products", "/knxprod", "--data", "/data", "--allow-any-bind"]
