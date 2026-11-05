FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py schema.json source.json ./
RUN python fetch_ocr_template.py

ENV OMP_THREAD_LIMIT=1 OPENBLAS_NUM_THREADS=1 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENTRYPOINT ["/usr/bin/nice", "-n", "10", "python", "-u", "/app/production.py"]
CMD ["run", "--directory", "/data", "--workers", "2"]
