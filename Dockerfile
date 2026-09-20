FROM python:3.11-slim AS builder

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --no-compile --target /deps -r requirements.txt


FROM gcr.io/distroless/python3-debian12

COPY --from=builder /deps /deps
COPY . /app

ENV PYTHONPATH=/deps \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
USER 1000

CMD ["/app/bot.py"]
