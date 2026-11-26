# Cinema hall checks. See docs/KINOZAL.md
# Checks run in python:3.14-slim, the gate's Python, not in the bot's venv

.PHONY: help check lint format audit test lock

# The default target, so a bare make only prints the list
help:
	@echo 'make -C kinozal <target>   (from the repository root; inside kinozal/ just make <target>)'
	@echo ''
	@echo 'Checks, before a merge of a hall change:'
	@echo '  check       Lint + test'
	@echo '  lint        The gate by ruff (lint and format), kinozal.sh by shellcheck'
	@echo '  format      Format the gate by ruff and apply its safe fixes, import order included'
	@echo '  audit       Known vulnerabilities in the gate'"'"'s dependencies'
	@echo '  test        The gate'"'"'s tests (OWNCAST_URL + OWNCAST_ADMIN_PASSWORD: the chat ones too)'
	@echo '  lock        Recompile gate/requirements*.txt after editing gate/requirements*.in'
	@echo ''
	@echo 'Owncast settings run on the server itself: ./kinozal.sh help'

# ==================================================================================================================
# checks
# ==================================================================================================================

PY_IMAGE := python:3.14-slim
GATE := $(CURDIR)/gate
LOCK_TOOLS := pip==26.3 pip-tools==7.7.0
RUFF := ruff==0.17.6
PIP_AUDIT := pip-audit==2.10.1
SHELLCHECK := koalaman/shellcheck:v0.11.0
TOOLS_RUN = docker run --rm --user $$(id -u):$$(id -g) -e HOME=/tmp -e PIP_DISABLE_PIP_VERSION_CHECK=1

# Run before merging a hall change. The root make check skips kinozal/
check: lint test

# ruff for the gate, lint and format (rules in gate/pyproject.toml), shellcheck for kinozal.sh
lint:
	$(TOOLS_RUN) -v $(CURDIR):/w:ro -w /w $(PY_IMAGE) sh -c \
		'pip install -q --user --no-warn-script-location $(RUFF) && /tmp/.local/bin/ruff check --no-cache gate \
		&& /tmp/.local/bin/ruff format --check --no-cache gate'
	docker run --rm -v $(CURDIR):/mnt:ro $(SHELLCHECK) /mnt/kinozal.sh

# The same ruff as lint, writing into gate/
format:
	$(TOOLS_RUN) -v $(GATE):/w -w /w $(PY_IMAGE) sh -c \
		'pip install -q --user --no-warn-script-location $(RUFF) && /tmp/.local/bin/ruff check --no-cache --fix . \
		&& /tmp/.local/bin/ruff format --no-cache .'

# Audits the pinned versions as they are, --disable-pip resolves nothing
audit:
	$(TOOLS_RUN) -v $(GATE):/w:ro -w /w $(PY_IMAGE) sh -c \
		'pip install -q --user --no-warn-script-location $(PIP_AUDIT) \
		&& /tmp/.local/bin/pip-audit --disable-pip -r requirements.txt -r requirements-dev.txt'

# The chat tests also run when OWNCAST_URL and OWNCAST_ADMIN_PASSWORD point to a local Owncast
test:
	docker run --rm --network host -v $(GATE):/src:ro -e OWNCAST_URL -e OWNCAST_ADMIN_PASSWORD $(PY_IMAGE) sh -c \
		'cp -r /src /tmp/gate && cd /tmp/gate && pip install -q --root-user-action=ignore --disable-pip-version-check \
		--require-hashes -r requirements-dev.txt && python -m pytest -q -p no:cacheprovider'

# Rebuild gate/requirements*.txt with hashes after editing the .in files
lock:
	$(TOOLS_RUN) -v $(GATE):/w -w /w $(PY_IMAGE) sh -c \
		'pip install -q --user --no-warn-script-location $(LOCK_TOOLS) && L="/tmp/.local/bin/pip-compile -q \
		--generate-hashes --strip-extras --no-emit-index-url" && $$L -o requirements.txt requirements.in \
		&& $$L --allow-unsafe -o requirements-dev.txt requirements-dev.in'

