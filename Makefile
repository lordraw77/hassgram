# Hassgram -- build and publish the Docker image.
#
#   make build              build lordraw/hassgram:<version> and :latest locally
#   make push               publish :latest (multi-arch)
#   make release            publish :<git tag> and :latest (multi-arch)
#   make tag V=1.2.3        create and push the git tag, then release it
#
# The version always comes from git: `make release` refuses to run unless HEAD
# is exactly on an annotated tag, so a published :X.Y.Z is always reproducible.

IMAGE      ?= lordraw/hassgram
PLATFORMS  ?= linux/amd64
BUILDER    ?= hassgram-builder

# Version reported by the image itself: nearest tag, or short sha when untagged.
VERSION    := $(shell git describe --tags --always --dirty 2>/dev/null || echo dev)
# Set only when HEAD sits exactly on a tag -- this is what `release` publishes.
TAG        := $(shell git describe --tags --exact-match 2>/dev/null)
VCS_REF    := $(shell git rev-parse --short HEAD 2>/dev/null || echo unknown)
BUILD_DATE := $(shell date -u +%Y-%m-%dT%H:%M:%SZ)

BUILD_ARGS = --build-arg VERSION=$(1) \
           --build-arg VCS_REF=$(VCS_REF) \
           --build-arg BUILD_DATE=$(BUILD_DATE)

.PHONY: help version test build run push release tag login buildx check-tag clean

help:
	@grep -hE '^[a-z][a-zA-Z0-9_-]*:.*?## ' $(MAKEFILE_LIST) | sed -E 's/^([a-z-]+):.*## /\1\t/' | expand -t 12

version: ## Print the version this checkout would publish
	@echo "image   : $(IMAGE)"
	@echo "version : $(VERSION)"
	@echo "git tag : $(if $(TAG),$(TAG),<none: HEAD is not on a tag>)"
	@echo "revision: $(VCS_REF)"

test: ## Run the unit test suite
	python3 -m unittest discover

build: ## Build the image locally, tagged :$(VERSION) and :latest
	docker build $(call BUILD_ARGS,$(VERSION)) \
		-t $(IMAGE):$(VERSION) -t $(IMAGE):latest .

run: build ## Run the freshly built image with the local .env
	docker run --rm --name hassgram --env-file .env $(IMAGE):$(VERSION)

login: ## Log in to Docker Hub
	docker login

buildx: ## Create the multi-arch builder if it is missing
	docker buildx inspect $(BUILDER) >/dev/null 2>&1 \
		|| docker buildx create --name $(BUILDER) --driver docker-container --use
	docker buildx use $(BUILDER)

push: buildx ## Build multi-arch and publish :latest
	docker buildx build --platform $(PLATFORMS) \
		$(call BUILD_ARGS,$(VERSION)) \
		-t $(IMAGE):latest --push .
	@echo "pushed $(IMAGE):latest ($(VERSION))"

check-tag:
	@$(if $(TAG),,$(error HEAD is not on a git tag -- run 'make tag V=X.Y.Z' first))

release: check-tag buildx ## Build multi-arch and publish :<git tag> and :latest
	docker buildx build --platform $(PLATFORMS) \
		$(call BUILD_ARGS,$(TAG)) \
		-t $(IMAGE):$(TAG) -t $(IMAGE):latest --push .
	@echo "pushed $(IMAGE):$(TAG) and $(IMAGE):latest"

tag: ## Create+push git tag V=X.Y.Z, then release it
	@$(if $(V),,$(error usage: make tag V=X.Y.Z))
	git tag -a "$(V)" -m "hassgram $(V)"
	git push origin "$(V)"
	$(MAKE) release

clean: ## Remove the locally built images
	-docker image rm $(IMAGE):$(VERSION) $(IMAGE):latest 2>/dev/null || true
