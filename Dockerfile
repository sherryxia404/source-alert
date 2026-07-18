FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY source_alert.py sources.yaml ./
RUN mkdir -p /app/data

CMD ["python", "source_alert.py"]
