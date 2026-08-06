include tags.env

LOCATION ?= us-central1
REPO ?= $(eval REPO := $(LOCATION)-docker.pkg.dev/$(shell gcloud config get core/project)/kube-agents)$(REPO)

BAD_SKILLS := $(wildcard agents/*/defaults/skills/*)

.PHONY: default help docker-build docker-build-agents docker-build-credential-proxy docker-push docker-push-agents docker-push-credential-proxy dev-rebuild-agent status prettier-check prettier-write test-python validate docs-generate docs-check docs-check-generated docs-check-links docs-check-terminology docs-check-map chart-sync chart-check

# The agent images this repository builds -- one per `--target` stage in
# deploy/docker/Dockerfile, which is not the same thing as one per directory
# under agents/. This was `$(wildcard agents/*/)`, and every `make` at the
# repository root failed on the first stage it invented:
# `target stage "chat" could not be found`. There is no chat or cluster image.
# agents/chat/ is baked into this image as /opt/defaults (it is the `default`
# profile) and agents/cluster/ as /opt/cluster-template, which the Platform
# Agent scaffolds per cluster at runtime. Adding a genuinely new image means
# adding a Dockerfile stage, so name them here rather than guessing.
AGENTS := platform


default: docker-build

help: ## Display this help.
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage:\n  make \033[36m<target>\033[0m\n\n"} /^[a-zA-Z_0-9][a-zA-Z_0-9 -]*:.*##/ { printf "  \033[36m%-28s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

# Docker builds
docker-build: docker-build-agents docker-build-credential-proxy ## Build every image in deploy/docker/Dockerfile (the default target).
docker-build-agents: $(foreach agent,$(AGENTS),docker-build-$(agent)) ## Build the agent images (see the AGENTS variable).

.PHONY: $(foreach agent,$(AGENTS),docker-build-$(agent))
$(foreach agent,$(AGENTS),docker-build-$(agent)): docker-build-%:
	docker build --build-arg HERMES_AGENT_TAG=$(HERMES_AGENT_TAG) --target $* -t $(REPO)/$*-agent:latest -f deploy/docker/Dockerfile .

docker-build-credential-proxy: ## Build the credential-proxy sidecar image.
	docker build --build-arg HERMES_AGENT_TAG=$(HERMES_AGENT_TAG) --target credential-proxy -t $(REPO)/credential-proxy:latest -f deploy/docker/Dockerfile .

# Docker pushes
docker-push: docker-push-agents docker-push-credential-proxy ## Build and push every image to $$REPO.
docker-push-agents: $(foreach agent,$(AGENTS),docker-push-$(agent)) ## Build and push the agent images.

.PHONY: $(foreach agent,$(AGENTS),docker-push-$(agent))
$(foreach agent,$(AGENTS),docker-push-$(agent)): docker-push-%: docker-build-%
	docker push $(REPO)/$*-agent:latest

docker-push-credential-proxy: docker-build-credential-proxy ## Build and push the credential-proxy image.
	docker push $(REPO)/credential-proxy:latest

dev-rebuild-agent: ## Fast local iteration: rebuild and redeploy an agent image (e.g. make dev-rebuild-agent ARGS="platform").
	@$(MAKE) -C k8s-operator dev-rebuild-agent ARGS="$(ARGS)"


status: ## Show the working tree status.
	git status

prettier-check: ## Check Markdown/YAML formatting (CI runs this).
	npx prettier --check "**/*.md" "**/*.yaml" "**/*.yml"

prettier-write: ## Reformat all Markdown/YAML in place.
	npx prettier --write "**/*.md" "**/*.yaml" "**/*.yml"

# Unit tests for every Python helper outside k8s-operator/, which has its own
# target. Mostly stdlib-only -- the skill helpers shell out to gh/kubectl
# rather than importing SDKs -- but the agent scripts do import a few third
# party packages; CI installs those (.github/workflows/python-tests.yml) and
# a local run needs them on the path too.
#
# The wildcards are what keep this honest: a new skill's tests are picked up
# without editing this file. Six globs rather than one because the tests do
# not all live under skills -- the agent scripts the skills share, the Chat
# Agent plugins, the image patches, the image build itself and the
# repository's own tooling in scripts/ each hold their own. scripts/ is here
# because it was not: the tests for the upstream-skill sync sat outside every
# glob, so they had never once run in CI. Discovery is then run once per
# directory rather than once over the tree, because none of them are packages
# -- `unittest discover` pointed at agents/platform/skills finds nothing and
# still exits 0, which reads as a passing suite. That also keeps
# deploy/docker and deploy/docker/patches separate, which they must be: the
# patch tests import their subject by bare module name, which only resolves
# with their own directory as the discovery root.
PYTHON_TEST_DIRS := $(sort $(dir \
	$(wildcard agents/*/skills/*/scripts/test_*.py) \
	$(wildcard agents/*/scripts/test_*.py) \
	$(wildcard agents/*/defaults/plugins/*/test_*.py) \
	$(wildcard deploy/docker/test_*.py) \
	$(wildcard deploy/docker/patches/test_*.py) \
	$(wildcard scripts/test_*.py)))

test-python: ## Run the Python unit tests outside k8s-operator/.
	@if [ -z "$(PYTHON_TEST_DIRS)" ]; then \
		echo "Error: no test_*.py files found under agents/, deploy/docker or scripts/."; \
		echo "Either the tests moved or the globs are stale -- failing rather than reporting success."; \
		exit 1; \
	fi
	@set -e; for dir in $(PYTHON_TEST_DIRS); do \
		echo "==> $$dir"; \
		(cd $$dir && python3 -m unittest discover -p "test_*.py"); \
	done

# Documentation tables that mirror a machine-readable source (cron jobs, the
# skill catalogue, the provisioning steps) are generated rather than hand-kept.
docs-generate: ## Regenerate the <!-- BEGIN GENERATED --> doc regions from their sources.
	@python3 scripts/generate_docs.py

# Everything CI enforces about the docs, in one command.
docs-check: docs-check-generated docs-check-links docs-check-terminology docs-check-map ## Run every documentation check CI runs.

docs-check-generated:
	@python3 scripts/generate_docs.py --check

docs-check-links:
	@python3 scripts/check_docs_links.py

docs-check-terminology:
	@./hack/check-docs-terminology.sh

docs-check-map:
	@python3 scripts/check_docs_map.py

chart-sync: ## Sync the Helm chart's CRD copies and operator ClusterRole rules from k8s-operator/config.
	@./hack/sync-chart-manifests.sh

chart-check: ## Verify the chart's CRD/RBAC copies match k8s-operator/config (CI runs this).
	@./hack/sync-chart-manifests.sh --check

validate: ## Fail if any skill sits under agents/*/defaults/skills/.
	@if [ -n "$(BAD_SKILLS)" ]; then \
		echo "Error: Skills should not be placed under agents/*/defaults/skills. Move them to agents/*/skills/"; \
		set -- $(BAD_SKILLS); \
		for file; do echo "  $$file"; done; \
		exit 1; \
	else \
		echo "Validation passed: No skills found in invalid paths."; \
	fi



