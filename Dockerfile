# Free-tier friendly image: CPU only, no GPU, models baked in so a cold start does not download anything.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app/src FASTEMBED_CACHE_PATH=/app/models PORT=7860
WORKDIR /app

COPY pyproject.toml ./
RUN pip install --no-cache-dir requests beautifulsoup4 lxml numpy fastembed

# search models (about 150 MB), fetched once at build time
RUN python - <<'PY'
from fastembed import TextEmbedding
from fastembed.rerank.cross_encoder import TextCrossEncoder
TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
TextCrossEncoder(model_name="Xenova/ms-marco-MiniLM-L-6-v2")
PY

COPY src ./src
COPY deploy/data ./data

# Hugging Face Spaces runs the container as uid 1000
RUN useradd -m -u 1000 app && chown -R app /app
USER app
EXPOSE 7860
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:%s/health'%os.environ.get('PORT','7860'))"
CMD ["python", "-m", "edgar_analyst", "serve"]
