FROM python:3.11-slim-bookworm AS builder

WORKDIR /build

COPY requirements.txt .

RUN pip install --no-cache-dir --no-compile --require-hashes --target /deps -r requirements.txt

# ruaccent's models (ё and word stress for the voice) are downloaded here: the bot then
# starts without the network, and /deps, where ruaccent keeps part of them, is read-only
COPY src/discord/local/voice/stress.py .
RUN PYTHONPATH=/deps python -c "import pathlib, stress; stress.load(pathlib.Path('/opt/ruaccent'))"
# What its tiny mode never reads (350 MB): the rule engine with its morphology, the stress
# usage model, the full dictionary. The emptied koziev directory must stay, or load()
# downloads it again
RUN rm -rf /deps/ruaccent/koziev/* /opt/ruaccent/.cache /opt/ruaccent/nn/nn_stress_usage_predictor \
        /opt/ruaccent/dictionary/accents.json.gz /opt/ruaccent/dictionary/rule_engine \
    && PYTHONPATH=/deps HF_HUB_OFFLINE=1 python -c "import pathlib, stress; stress.load(pathlib.Path('/opt/ruaccent')); print(stress.apply('Все пришли, и все было хорошо.', True))"

# libopus encodes the Discord voice; distroless has no package manager, so the library is
# taken from Debian here (cp follows the .so.0 symlink to the real file)
RUN apt-get update \
    && apt-get install -y --no-install-recommends libopus0 \
    && mkdir /opus && cp /usr/lib/*-linux-gnu/libopus.so.0 /opus/ \
    && rm -rf /var/lib/apt/lists/*


FROM gcr.io/distroless/python3-debian12

COPY --from=builder /deps /deps
COPY --from=builder /opus/libopus.so.0 /opt/opus/libopus.so.0
COPY --from=builder /opt/ruaccent /opt/ruaccent
COPY . /app

ENV PYTHONPATH=/deps \
    DISCORD_OPUS_LIB=/opt/opus/libopus.so.0 \
    BOT_ACCENT_PATH=/opt/ruaccent \
    HF_HUB_OFFLINE=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

USER 1000:1000

CMD ["/app/bot.py"]
