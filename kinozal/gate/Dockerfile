FROM python:3.14-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir --require-hashes -r requirements.txt \
    && useradd --system --uid 10001 gate \
    && mkdir /data && chown gate /data

COPY src/gate/ gate/

USER gate

EXPOSE 8000

CMD ["uvicorn", "gate.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header", "--no-access-log"]
