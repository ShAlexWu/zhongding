FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends libreoffice-writer fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
RUN pip install --no-cache-dir .
COPY app ./app

EXPOSE 50012
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "50012"]
