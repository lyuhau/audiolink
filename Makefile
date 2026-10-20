# Developer commands: build, test, and the dev loops on a dev Pi. Each target only
# calls a script (the logic lives there), and every target always runs: no file
# tracking, so nothing is ever "up to date" when it isn't (a card has too many inputs
# to list). Anything a user does (setting up the Pi, writing or backing up its card)
# is the audiolink command itself, not a target here.
#   make help

PYTHON ?= python3
VERSION = $(shell $(PYTHON) -c "import audiolink; print(audiolink.__version__)")

.PHONY: help pyz app modules card release-card test deploy hot smoke relay-deploy

help:          ## list the targets
	@grep -E '^[a-z-]+: .*## ' $(MAKEFILE_LIST) | sed -E 's/:.*## /\t/' | expand -t 16

# ---- build
pyz:           ## dist/audiolink.pyz (what releases ship and the Pi runs)
	$(PYTHON) build_pyz.py

app:           ## the standalone app for this OS, dist/audiolink-OS-ARCH.zip (needs pyinstaller)
	$(PYTHON) build_bin.py

modules:       ## the platform's prebuilt gadget kernel modules, once (cards need them)
	platform/alpine-module/fetch.sh

card:          ## the dev card image: build/card-pi/ (USB ethernet, SSH, debug console)
	tools/build_card.sh config/pi.toml

release-card:  ## the release card image, as CI builds it: build/card-pi-release/, dist/audiolink-pi-VER.zip
	tools/build_card.sh config/pi-release.toml
	$(PYTHON) tools/card_zip.py build/card-pi-release dist/audiolink-pi-$(VERSION).zip

# ---- test
test:          ## the test suite (no Pi needed)
	$(PYTHON) -m unittest discover -s tests -t .

# ---- dev loops: a dev Pi plugged in (the dev card), reached over USB ethernet
deploy: card   ## the dev card onto the running dev Pi, then reboot it
	tools/deploy.sh

hot:           ## just the current code onto the running Pi, no reboot (lost at the next boot)
	tools/hot.sh

smoke:         ## end-to-end check of the running host and the Pi behind it
	tools/smoke.sh

relay-deploy:  ## the current code onto the relay server (RELAY=root@HOST)
	tools/relay_deploy.sh
