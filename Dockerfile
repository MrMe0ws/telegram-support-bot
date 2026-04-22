FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY texts ./texts
COPY app ./app

ENV TEXTS_DIR=/app/texts

ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "app.main"]
