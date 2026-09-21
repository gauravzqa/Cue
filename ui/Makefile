# The dock. Everything here works with Command Line Tools only -- no Xcode.

.DEFAULT_GOAL := help
SHELL := /bin/bash

help:
	@echo "make build      compile (debug)"
	@echo "make test       run the core test suite"
	@echo "make app        assemble and sign build/daa.app"
	@echo "make run        build the app and launch it"
	@echo "make demo       run the app against tools/fake-bridge (no keys, no network)"
	@echo "make dr         print the designated requirement (the TCC identity)"
	@echo "make gen-tests  regenerate the test registration list"
	@echo "make lint       check that no policy vocabulary leaked into Swift"
	@echo "make clean"

build:
	swift build

test:
	swift run daadock-tests $(FILTER)

gen-tests:
	python3 tools/gen-tests.py

app:
	./tools/make-app.sh

run: app
	open build/daa.app

# Drive the whole UI with a fake Python peer: no keys, no network, no real
# tools, and a real approval card. This is how to look at the card without
# running anything that could act on the machine.
demo: app
	DAA_PYTHON="$(PWD)/tools/fake-bridge" \
	DAA_REPO_ROOT="$(PWD)" \
	build/daa.app/Contents/MacOS/daadock

# The line that decides whether TCC grants survive a rebuild. Run it twice
# across two builds: under an ad-hoc signature it changes, under a certificate
# it does not.
dr:
	@codesign --display -r - build/daa.app 2>&1 | sed -n 's/^# designated => //p'

# Risk 2 from the plan: "Swift becomes a place where policy hides." The Swift
# side must hold no tier, no threshold and no allowlist -- it renders state and
# forwards clicks. This greps for the vocabulary that would mean otherwise.
# Comments are stripped first, so a doc comment explaining the rule is fine and
# a line of code implementing one is not.
lint:
	@bad=$$(find Sources/DaaDock Sources/DaaDockCore -name '*.swift' \
	        -exec sed -e 's://.*::' -e '/^ *\*/d' {} + \
	        | grep -nE '\b(RiskTier|blastRadius *[<>=]|allowlist|allowList|isSafe|autoApprove|auto_approve|shouldSkipConfirm)\b' \
	        || true); \
	if [ -n "$$bad" ]; then \
	  echo "policy vocabulary found in Swift sources:"; echo "$$bad"; exit 1; \
	else echo "ok: no policy in Swift"; fi

clean:
	swift package clean
	rm -rf build .build

.PHONY: help build test gen-tests app run demo dr lint clean
