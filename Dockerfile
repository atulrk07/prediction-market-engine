FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt setuptools==84.0.0

COPY pyproject.toml README.md alembic.ini ./
COPY *.py dashboard.html ./
COPY migrations ./migrations
RUN python -m pip install --no-cache-dir --no-build-isolation --no-deps . \
    && useradd --create-home --uid 10001 app
USER app

CMD ["python", "-m", "uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-keep-alive", "5", "--timeout-graceful-shutdown", "10"]
