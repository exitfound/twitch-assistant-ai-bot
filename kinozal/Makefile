# The cinema hall's commands (docs/KINOZAL.md): make -C kinozal <target> from the repository root, make <target>
# here; a bare make lists them. The checks run in python:3.14-slim, the gate's Python, not in the bot's venv.

.PHONY: help check lint audit test lock deploy setup style status link stream-key

# The target list; the default target, so a bare make never runs anything
help:
	@echo 'make -C kinozal <target>   (from the repository root; inside kinozal/ just make <target>)'
	@echo ''
	@echo 'Checks, before a merge of a hall change:'
	@echo '  check       lint + test'
	@echo '  lint        the gate by ruff, kinozal.sh by shellcheck'
	@echo '  audit       known vulnerabilities in the gate'"'"'s dependencies'
	@echo '  test        the gate'"'"'s tests (OWNCAST_URL + OWNCAST_ADMIN_PASSWORD: the chat ones too)'
	@echo '  lock        recompile gate/requirements*.txt after editing gate/requirements*.in'
	@echo ''
	@echo 'The host (SSH access from the bot'"'"'s .env):'
	@echo '  deploy      upload kinozal/ and restart what changed; the gate waits while a session is open'
	@echo '  setup       every Owncast setting; refused while a stream is live'
	@echo '  style       only the page'"'"'s CSS, JS and offline text; safe during a stream'
	@echo '  status      containers, session, stream'
	@echo '  link        the OBS chat dock link'
	@echo '  stream-key  the stream key for OBS'

# ==================================================================================================================
# checks: the gate's code, tests and dependencies, kinozal.sh by shellcheck
# ==================================================================================================================

PY_IMAGE := python:3.14-slim
GATE := $(CURDIR)/gate
LOCK_TOOLS := pip==26.3 pip-tools==7.7.0
RUFF := ruff==0.17.6
PIP_AUDIT := pip-audit==2.10.1
SHELLCHECK := koalaman/shellcheck:v0.11.0
TOOLS_RUN = docker run --rm --user $$(id -u):$$(id -g) -e HOME=/tmp -e PIP_DISABLE_PIP_VERSION_CHECK=1

# Before a merge of a hall change: the root make check leaves kinozal/ out
check: lint test

# The gate by ruff (kinozal/ruff.toml), kinozal.sh by shellcheck
lint:
	$(TOOLS_RUN) -v $(CURDIR):/w:ro -w /w $(PY_IMAGE) sh -c \
		'pip install -q --user --no-warn-script-location $(RUFF) && /tmp/.local/bin/ruff check --no-cache gate'
	docker run --rm -v $(CURDIR):/mnt:ro $(SHELLCHECK) /mnt/kinozal.sh

# Known vulnerabilities in the gate's dependencies, audited as pinned (--disable-pip resolves nothing)
audit:
	$(TOOLS_RUN) -v $(GATE):/w:ro -w /w $(PY_IMAGE) sh -c \
		'pip install -q --user --no-warn-script-location $(PIP_AUDIT) \
		&& /tmp/.local/bin/pip-audit --disable-pip -r requirements.txt -r requirements-dev.txt'

# The gate's tests; with OWNCAST_URL and OWNCAST_ADMIN_PASSWORD, the chat ones against a local Owncast too
test:
	docker run --rm --network host -v $(GATE):/src:ro -e OWNCAST_URL -e OWNCAST_ADMIN_PASSWORD $(PY_IMAGE) sh -c \
		'cp -r /src /tmp/gate && cd /tmp/gate && pip install -q --root-user-action=ignore --disable-pip-version-check \
		--require-hashes -r requirements-dev.txt && python -m pytest -q -p no:cacheprovider'

# Recompiles gate/requirements*.txt with hashes after an edit of gate/requirements*.in
lock:
	$(TOOLS_RUN) -v $(GATE):/w -w /w $(PY_IMAGE) sh -c \
		'pip install -q --user --no-warn-script-location $(LOCK_TOOLS) && L="/tmp/.local/bin/pip-compile -q \
		--generate-hashes --strip-extras --no-emit-index-url" && $$L -o requirements.txt requirements.in \
		&& $$L --allow-unsafe -o requirements-dev.txt requirements-dev.in'

# ==================================================================================================================
# the host: kinozal.sh with the SSH access from the bot's .env; outward-facing, so only on the streamer's yes
# ==================================================================================================================

# Uploads kinozal/ and restarts what changed; a changed gate waits while a session is open
deploy:
	./kinozal.sh deploy

# Every Owncast setting; refused while a stream is live
setup:
	./kinozal.sh setup

# The page's CSS, JS and offline text only; safe during a stream
style:
	./kinozal.sh style

# Containers, session and stream on the host
status:
	./kinozal.sh status

# The OBS chat dock link; @ keeps the command line out, so the output is the link alone
link:
	@./kinozal.sh link

# The stream key for OBS, printed alone like the link
stream-key:
	@./kinozal.sh stream-key
