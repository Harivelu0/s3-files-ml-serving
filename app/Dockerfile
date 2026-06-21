FROM python:3.11-slim

WORKDIR /app

# Install CPU-only torch first — prevents pulling CUDA build (~800MB → ~300MB)
RUN pip install --no-cache-dir \
    torch --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY templates/ templates/

ENV ARTIFACTS_DIR=/mnt/artifacts

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
