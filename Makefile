include tags.env

LOCATION ?= us-central1
REPO ?= $(eval REPO := $(LOCATION)-docker.pkg.dev/$(shell gcloud config get core/project)/kube-agents)$(REPO)

BAD_SKILLS := $(wildcard agents/*/defaults/skills/*)

.PHONY: cloud-build-push default docker-build docker-build-agents docker-build-credential-proxy docker-push docker-push-agents docker-push-credential-proxy dev-rebuild-agent status prettier-check prettier-write validate

AGENTS := $(notdir $(patsubst %/,%,$(wildcard agents/*/)))


default: docker-build

# Docker builds
docker-build: docker-build-agents docker-build-credential-proxy
docker-build-agents: $(foreach agent,$(AGENTS),docker-build-$(agent))

.PHONY: $(foreach agent,$(AGENTS),docker-build-$(agent))
$(foreach agent,$(AGENTS),docker-build-$(agent)): docker-build-%:
	docker build --build-arg HERMES_AGENT_TAG=$(HERMES_AGENT_TAG) --target $* -t $(REPO)/$*-agent:latest -f deploy/docker/Dockerfile .

docker-build-credential-proxy:
	docker build --build-arg HERMES_AGENT_TAG=$(HERMES_AGENT_TAG) --target credential-proxy -t $(REPO)/credential-proxy:latest -f deploy/docker/Dockerfile .

# Docker pushes
docker-push: docker-push-agents docker-push-credential-proxy
docker-push-agents: $(foreach agent,$(AGENTS),docker-push-$(agent))

.PHONY: $(foreach agent,$(AGENTS),docker-push-$(agent))
$(foreach agent,$(AGENTS),docker-push-$(agent)): docker-push-%: docker-build-%
	docker push $(REPO)/$*-agent:latest

docker-push-credential-proxy: docker-build-credential-proxy
	docker push $(REPO)/credential-proxy:latest

dev-rebuild-agent: ## Fast local iteration: rebuild and redeploy an agent image (e.g. make dev-rebuild-agent ARGS="platform").
	@$(MAKE) -C k8s-operator dev-rebuild-agent ARGS="$(ARGS)"

# Cloud Build produces amd64 images regardless of the host architecture, so this is the path to
# use from an arm64 machine (Apple silicon) — a local `docker build` there yields images the GKE
# nodes cannot run. It also pushes straight into the project's Artifact Registry.
.PHONY: cloud-build-push
cloud-build-push: ## Build+push all agent images via Cloud Build (LOCATION and TAG overridable).
	@set -e; \
	. ./tags.env; \
	TAG=$${TAG:-src-$$(git rev-parse --short HEAD)}; \
	REPO=$(LOCATION)-docker.pkg.dev/$$(gcloud config get core/project 2>/dev/null)/kube-agents; \
	echo "Building into $$REPO with tag $$TAG"; \
	for target in $(AGENTS) credential-proxy; do \
	  case $$target in credential-proxy) name=credential-proxy ;; *) name=$$target-agent ;; esac; \
	  echo ">>> $$name"; \
	  gcloud builds submit --config deploy/docker/cloudbuild.yaml \
	    --substitutions=_IMAGE_URI=$$REPO/$$name:$$TAG,_IMAGE_URI_LATEST=$$REPO/$$name:latest,_TARGET=$$target,_HERMES_AGENT_TAG=$$HERMES_AGENT_TAG; \
	done; \
	echo "Done. Set AGENT_IMAGE=$$REPO/platform-agent and AGENT_TAG=$$TAG in k8s-operator/scripts/vars.sh"


status:
	git status

prettier-check:
	npx prettier --check "**/*.md" "**/*.yaml" "**/*.yml"

prettier-write:
	npx prettier --write "**/*.md" "**/*.yaml" "**/*.yml"

validate:
	@if [ -n "$(BAD_SKILLS)" ]; then \
		echo "Error: Skills should not be placed under agents/*/defaults/skills. Move them to agents/*/skills/"; \
		set -- $(BAD_SKILLS); \
		for file; do echo "  $$file"; done; \
		exit 1; \
	else \
		echo "Validation passed: No skills found in invalid paths."; \
	fi



