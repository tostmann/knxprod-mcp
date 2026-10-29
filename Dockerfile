# knxprod-mcp: MCP server for KNX manufacturer product files (.knxprod)
FROM python:3.13.18-slim-trixie

LABEL org.opencontainers.image.title="knxprod-mcp" \
      org.opencontainers.image.description="MCP server for KNX manufacturer product files (.knxprod)" \
      org.opencontainers.image.licenses="MIT"

# Defaults for the container. Every KNXPROD_* variable can be overridden
# (docker run -e, Compose environment). With host networking set
# KNXPROD_BIND=auto or an address, so the server does not listen on every
# interface of the host.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    KNXPROD_DIR=/knxprod \
    KNXPROD_DATA=/data \
    KNXPROD_BIND=0.0.0.0 \
    KNXPROD_ALLOW_ANY_BIND=1 \
    KNXPROD_PORT=8095

RUN groupadd --system --gid 10001 knxprod \
 && useradd --system --uid 10001 --gid 10001 --home-dir /nonexistent --no-create-home \
            --shell /usr/sbin/nologin knxprod \
 && mkdir -p /knxprod /data \
 && chown 10001:10001 /knxprod /data

COPY knxprod_mcp.py LICENSE /app/
COPY skill/SKILL.md /app/skill/

USER 10001:10001
VOLUME ["/knxprod", "/data"]
EXPOSE 8095

# The server listens at once and indexes the product folder in the background;
# /healthz says how many files are still to be read.
# "health" asks the running server, wherever it listens.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5m --retries=3 \
  CMD ["python", "/app/knxprod_mcp.py", "health"]

ENTRYPOINT ["python", "/app/knxprod_mcp.py"]
CMD ["serve"]
