FROM python:3.11-slim-bookworm AS builder

WORKDIR /build

COPY requirements.txt .

RUN pip install --no-cache-dir --no-compile --require-hashes --target /deps -r requirements.txt

# libopus encodes the Discord voice; distroless has no package manager, so the library is
# taken from Debian here (cp follows the .so.0 symlink to the real file)
RUN apt-get update \
    && apt-get install -y --no-install-recommends libopus0 \
    && mkdir /opus && cp /usr/lib/*-linux-gnu/libopus.so.0 /opus/ \
    && rm -rf /var/lib/apt/lists/*


FROM gcr.io/distroless/python3-debian12

COPY --from=builder /deps /deps
COPY --from=builder /opus/libopus.so.0 /opt/opus/libopus.so.0
COPY . /app

ENV PYTHONPATH=/deps \
    DISCORD_OPUS_LIB=/opt/opus/libopus.so.0 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

USER 1000:1000

CMD ["/app/bot.py"]
