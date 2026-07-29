FROM python:3.10-slim

WORKDIR /app

# Install uv first
RUN pip install uv

# Copy project files
COPY . .

# Install the locked runtime dependencies
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"

# Expose the port used by the MCP server (default 4200, can be overridden by PORT env)
EXPOSE 4200
ENV PORT=4200
ENV WEIBO_COOKIE_FILE=/data/cookies.json
VOLUME ["/data"]

# Persist login with a named volume, then run: docker run ... IMAGE login
ENTRYPOINT ["mcp-server-weibo"]
CMD ["http"]
