# Dev / CI helpers. `mpdris2/__init__.py` is the version source of truth;
# this Makefile drives lint / test / build / deb / i18n and keeps the
# debian/changelog in sync with the Python version, no logic duplicated
# in the workflow YAML.

PYTHON  ?= python3
VERSION := $(PYTHON) scripts/version.py

.PHONY: version deb-version check-tag sync-deb \
        lint lint-ruff lint-mypy test build deb clean \
        i18n-extract i18n-compile

# --- version helpers ---------------------------------------------------

version:
	@$(VERSION)

deb-version:
	@$(VERSION) --debian

# Fail if the git tag doesn't match __init__.py (vX prefix optional).
# CI invokes this with TAG=$GITHUB_REF_NAME on tag pushes to catch a drift
# between the manual __init__.py bump and the tag.
TAG ?= $(GITHUB_REF_NAME)
check-tag:
	@$(VERSION) --check-tag '$(TAG)'

# Bump debian/changelog to match deb-version. Idempotent — noop if already
# in sync. Needs `devscripts` (dch) and `dpkg-dev` (dpkg-parsechangelog).
sync-deb:
	@deb=$$($(VERSION) --debian); \
	cl=$$(dpkg-parsechangelog -S Version); \
	if [ "$$deb" != "$$cl" ]; then \
		dch -b --newversion "$$deb" --distribution unstable \
			--urgency medium "Release $$deb"; \
	fi

# --- dev workflow ------------------------------------------------------

lint: lint-ruff lint-mypy

lint-ruff:
	ruff check mpdris2/ tests/

lint-mypy:
	mypy mpdris2/

test:
	pytest -q

build:
	$(PYTHON) -m build

# Builds the .deb via dpkg-buildpackage. Requires a Debian toolchain
# (debhelper, dh-python, devscripts, etc.) — not available on Fedora;
# use a Debian container for local builds. Note: this target does NOT
# call `sync-deb`; call it first manually for a release build so the
# changelog matches __init__.py.
deb:
	dpkg-buildpackage -b -us -uc

clean:
	rm -rf build/ dist/ *.egg-info mpdris2.egg-info mpdris2/locale/

# --- i18n --------------------------------------------------------------

# Refresh the .pot template from current source.
i18n-extract:
	pybabel extract -F babel.cfg \
		--project=mpDris2 \
		--version="$$($(VERSION))" \
		--copyright-holder="Mathieu Réquillart" \
		--msgid-bugs-address=https://github.com/b0bbywan/mpDris2/issues \
		-o po/mpdris2.pot mpdris2/

# Compile .po files into the package's runtime locale tree.
i18n-compile:
	@for po in po/*.po; do \
		lang=$$(basename $$po .po); \
		mkdir -p mpdris2/locale/$$lang/LC_MESSAGES; \
		msgfmt $$po -o mpdris2/locale/$$lang/LC_MESSAGES/mpdris2.mo; \
	done
