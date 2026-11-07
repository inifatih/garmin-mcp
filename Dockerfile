FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

ENV PORT=8000 \
    GARMIN_MCP_TRANSPORT=http \
    HOST=0.0.0.0
EXPOSE 8000

# Required at runtime: GARMIN_MCP_TOKEN (shared secret) and GARMINTOKENS (token JSON from
# `garmin-mcp-login --print-tokens`, or mount a volume at /root/.garminconnect).
CMD ["garmin-mcp"]
