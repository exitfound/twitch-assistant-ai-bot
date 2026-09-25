# Everyday commands. There is no CI: `make check` is what runs before a merge.
PY := ./venv/bin/python3

.PHONY: venv lint fix test check audit lock up restart logs backup oauth

LOCK := $(PY) -m piptools compile --generate-hashes --strip-extras --no-emit-index-url

venv:
	rm -rf venv
	python3.11 -m venv venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install --require-hashes -r requirements.txt -r requirements-dev.txt

lint:
	$(PY) -m ruff check .

fix:
	$(PY) -m ruff check . --fix

test:
	$(PY) -m pytest

check: lint test

audit:
	$(PY) -m pip_audit -r requirements.txt -r requirements-dev.txt

lock:
	$(LOCK) -o requirements.txt requirements.in
	$(LOCK) --allow-unsafe -o requirements-dev.txt requirements-dev.in

up:
	docker compose up -d --build

restart:
	docker compose restart bot

logs:
	docker compose logs -f --tail=200 bot

backup:
	docker compose run --rm bot /app/bot.py --backup /data/chat_history.backup-$$(date +%F-%H%M).db

# New Twitch tokens. The container publishes no port – twitchio's OAuth adapter listens on
# localhost inside it – so the bot runs on the host for the login, from data/, where the
# container keeps .tio.tokens.json and the database (named explicitly: the default is the
# repository root). The shell catches Ctrl+C for itself, so the container comes back up
# after the bot stops. `up -d` rather than `start`: a changed .env reaches only a
# recreated container
oauth:
	@echo "Бот в контейнере останавливается: два процесса на одной паре токенов разлогинят друг друга."
	@echo "Открой ссылку из лога, войди нужным аккаунтом, затем Ctrl+C – контейнер поднимется сам."
	docker compose stop bot
	@trap "true" INT; cd data && BOT_DB_PATH=chat_history.db ../venv/bin/python3 ../bot.py; cd .. && docker compose up -d bot
