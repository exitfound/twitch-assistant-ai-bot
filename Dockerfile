# Bookworm, like the distroless runtime: a package built from source here links against
# the same glibc it will run on
FROM python:3.11-slim-bookworm AS builder

WORKDIR /build

COPY requirements.lock .

# The lock pins every package down to its dependencies, and each download is checked by hash
RUN pip install --no-cache-dir --no-compile --require-hashes --target /deps -r requirements.lock


FROM gcr.io/distroless/python3-debian12

COPY --from=builder /deps /deps
COPY . /app

ENV PYTHONPATH=/deps \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# A group too: without it the process runs with gid 0 and the database files it creates
# on the volume belong to root's group
USER 1000:1000

CMD ["/app/bot.py"]
