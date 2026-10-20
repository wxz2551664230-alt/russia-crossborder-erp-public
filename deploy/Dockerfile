FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml /app/
COPY backend /app/backend
RUN pip install --no-cache-dir .

COPY alembic.ini /app/
COPY frontend/dist /app/frontend/dist

EXPOSE 8788
CMD ["python", "-m", "uvicorn", "erp.main:app", "--host", "0.0.0.0", "--port", "8788"]
