# Kepler MCP server. Build: docker build -t kepler-mcp .   Run: see deploy/README.md
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONWARNINGS=ignore MPLBACKEND=Agg MPLCONFIGDIR=/tmp/mpl \
    TESSHUNT_MCP_DATA=/app/data/mcp XDG_CACHE_HOME=/app/data/.cache
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY tesshunt ./tesshunt
COPY tesshunt_mcp ./tesshunt_mcp
COPY kepler_sweep ./kepler_sweep
RUN pip install --no-cache-dir -e .
RUN useradd -m app && mkdir -p /app/data && chown -R app /app
USER app
VOLUME /app/data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=4).status==200 else 1)"
CMD ["python", "-m", "tesshunt_mcp.server", "--host", "0.0.0.0", "--port", "8000"]
