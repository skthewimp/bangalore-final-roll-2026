FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    numpy==2.4.0 scipy==1.16.3 pillow==12.1.0 \
    pymupdf==1.27.2.2 pandas==2.3.3 pyarrow==22.0.0

WORKDIR /app
ENV OMP_THREAD_LIMIT=1 OPENBLAS_NUM_THREADS=1
ENTRYPOINT ["python", "-u", "benchmark.py"]
