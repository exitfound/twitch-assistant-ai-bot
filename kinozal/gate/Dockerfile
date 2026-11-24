FROM python:3.14-slim

COPY requirements.txt /tmp/requirements.txt

RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt \
    && useradd --system --uid 10001 gate \
    && mkdir /data && chown gate /data

WORKDIR /app

COPY app.py .

USER gate

EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header", "--no-access-log"]
