FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.13 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src src
RUN uv sync --frozen --no-dev \
 && uv run playwright install --with-deps --only-shell chromium
COPY configs configs
COPY dbt dbt
ENV PATH="/app/.venv/bin:$PATH"
RUN useradd --create-home scraper && chown -R scraper /app
USER scraper
ENTRYPOINT ["scrapewatch"]
CMD ["run"]
