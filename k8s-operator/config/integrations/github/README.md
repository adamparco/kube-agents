# GitHub Token Minter (Minty) Integration

This directory contains the configuration and deployment manifests for integrating the **GitHub Token Minter (Minty)** broker into the cluster. This integration allows agents to securely request short-lived GitHub access tokens without storing long-lived, static credentials, enabling them to safely perform write operations on the Kubernetes infrastructure via GitOps.

## How It All Works

Minty acts as a secure broker between Google Cloud IAM (Workload Identity) and GitHub. When an agent requires access to a GitHub repository, the following flow occurs:

1. **The Request:** The agent initiates an HTTP request to the Minty service, specifying the target organization and repository. The request is authenticated using the agent's Google Service Account (GSA) OIDC token to cryptographically prove its identity.
2. **The Verification:** Minty evaluates the request against its local rules (provided by `configmap.yaml`). It extracts the `"email"` claim from the OIDC token and verifies it against the `assertion.email` rule. If the agent's email is authorized for the requested repository, the rule evaluates to true.
3. **The Exchange (KMS Signing):** Upon successful authorization, Minty interfaces with Google Cloud Key Management Service (KMS). Minty holds a reference to the GitHub App's private key stored securely in KMS. The private key is never exported or exposed to Minty. Instead, Minty constructs an authentication payload and invokes the KMS API to cryptographically sign it using secure hardware.
4. **The Token Generation:** Armed with the KMS-signed JWT, Minty authenticates with the GitHub API on behalf of the configured GitHub App. GitHub verifies the signature and returns a short-lived installation access token scoped to the target repository.
5. **The Delivery:** Minty returns this short-lived GitHub access token to the agent, which can then utilize it to perform write operations on the Kubernetes infrastructure via GitOps (e.g., by pushing configuration changes or managing Pull Requests).

## The GitHub App

Minty itself does not natively possess access to any GitHub repositories. The **GitHub App** serves as the machine identity within GitHub that holds the necessary permissions.

By installing the GitHub App into a target repository, explicit authorization is granted to that machine identity. Minty's role is strictly to ensure that only authorized internal workloads are permitted to generate tokens on behalf of the App.

### The organization requirement (read this first)

> [!IMPORTANT]
> **The target repository must live in a GitHub Organization, and the GitHub App must be owned
> by that same organization. A personal user account will not work.**

Minty resolves the installation with `InstallationForOrg`, i.e. `GET /orgs/{org}/installation`
(`pkg/server/source/github.go`; the call is unconditional and there is no configuration to make
it use the user or repo variant). GitHub's `/orgs/...` endpoints return `404` for personal
accounts, so against a user-owned repo every mint fails with:

```
error generating access token: errors retrieving GitHub installation:
failed to get access token url for org <name>: ... retryable status code: 404
```

This looks like a permissions or installation problem, but installing the App does not fix it —
the endpoint simply does not exist for user accounts. Two consequences:

- `GITHUB_ORG` must name a real organization. Check with `gh api /orgs/<name>`; a `404` there
  means it is a user account and Minty cannot work with it.
- The App must be **created under the organization**, not under your personal account. A
  private ("only on this account") App owned by a user cannot be installed on an org at all,
  and an org-owned App keeps the GitOps identity with the org rather than an individual.

Creating a free organization and moving the repo into it is the shortest path.

### Setting up the GitHub App

1. Go to **the organization's** settings -> **Developer settings** -> **GitHub Apps** ->
   **New GitHub App** (`https://github.com/organizations/<ORG>/settings/apps/new`).
2. Assign a name, set **Homepage URL** to the target repo, and disable **Webhook -> Active**
   (Minty never receives webhooks).
3. Under **Repository permissions** set `Contents: Read & write`, `Pull requests: Read & write`,
   `Metadata: Read-only` — these must be a superset of the `permissions:` in the scope rule in
   `configmap.yaml.template`.
4. Create the App and note the **App ID** (`GITHUB_APP_ID`).
5. Click **Generate a private key**; a `.pem` downloads. Point `GITHUB_PEM_PATH` at it. The key
   is shown exactly once — if you lose it, generate another.
6. Click **Install App** and install it on the target repository.

Verify the whole App before provisioning — this mints a JWT and asks GitHub who it is, which
catches a wrong App ID, a mismatched key, and a missing installation in one shot:

```bash
APP_ID=<your-app-id>; PEM=<path-to-pem>; ORG=<your-org>
b64url() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }
NOW=$(date +%s)
HDR=$(printf '{"alg":"RS256","typ":"JWT"}' | b64url)
PAY=$(printf '{"iat":%d,"exp":%d,"iss":"%s"}' $((NOW-60)) $((NOW+540)) "$APP_ID" | b64url)
JWT="$HDR.$PAY.$(printf '%s.%s' "$HDR" "$PAY" | openssl dgst -sha256 -sign "$PEM" | b64url)"

# Should print the app slug and an Organization owner:
curl -s -H "Authorization: Bearer $JWT" https://api.github.com/app | jq '{slug, owner: .owner.login, type: .owner.type}'
# Should return 200 — this is the exact call Minty makes:
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $JWT" \
  "https://api.github.com/orgs/${ORG}/installation"
```

Finally, the repository needs **at least one commit**. Agents deliver changes as pull requests,
and a repo with no default branch cannot accept one; `provision_10` warns when it finds none.

### Provisioning Configuration Variables

To deploy the agent with GitHub integration, the `vars.sh` file (used by the `provision.sh` script) must be populated with the details of your GitHub App.

- `GITHUB_APP_ID`: The unique numeric ID of the GitHub App (found in the App's General Settings).
- `GITHUB_ORG`: The name of the GitHub organization or user account where the repository is hosted.
- `GITHUB_REPO`: The name of the target repository the agent will manage.
- `GITHUB_PEM_PATH`: The absolute local file path to the downloaded `.pem` private key file. If provided, the provisioning script will automatically use the Minty CLI to import it into Google Cloud KMS. If omitted, the deployment will proceed but Minty will fail readiness probes until a key is manually imported.

## Minty Limitations & GSA Tokens

Minty was originally designed for integration with GitHub Actions, which inherently provides OIDC tokens containing a specific `"repository"` claim. Deploying Minty in GKE introduces specific constraints regarding this validation model:

- **KSA Tokens are Unsupported:** Native Kubernetes Service Account (KSA) tokens do not support the injection of arbitrary custom claims such as `"repository"`. Consequently, Minty's default validation engine will reject KSA tokens due to the missing claim.
- **GSA Tokens (The Solution):** To resolve this, Workload Identity is utilized to provide Google Service Account (GSA) OIDC tokens. Minty implements a specific exemption for tokens where the issuer is `https://accounts.google.com`. When processing a Google-issued token, Minty bypasses the `"repository"` claim requirement. Instead, it validates the caller's identity via the `assertion.email` rule and derives the target repository directly from the JSON POST payload.

## Cryptographic key import (openssl + gcloud)

`provision_10` imports the GitHub App `.pem` into Cloud KMS using only `openssl` and `gcloud`.
It creates the KMS import job if missing, waits for it to reach `ACTIVE` (a new job generates
its wrapping key asynchronously), converts the key, and imports it as a new key version.

GitHub issues the App key as **PKCS#1** (`BEGIN RSA PRIVATE KEY`) while KMS requires **PKCS#8**,
so the script converts it first; `gcloud kms keys versions import --target-key-file` then
performs the RSA-OAEP/AES wrapping client-side.

> [!NOTE]
> `--target-key-file` requires the `cryptography` library inside the gcloud SDK's own Python. On
> hosts where that is unavailable (and cannot be installed), do the wrapping with openssl and
> pass the finished blob to `--wrapped-key-file` instead. For `RSA_OAEP_3072_SHA256_AES_256` the
> payload is `RSA-OAEP-SHA256(ephemeral 256-bit AES key) || AES-KWP(target PKCS#8 DER)`:
>
> ```bash
> openssl pkcs8 -topk8 -inform PEM -outform DER -nocrypt -in app.pem -out target.der
> gcloud kms import-jobs describe "$JOB" --location="$REGION" --keyring="$KEYRING" \
>   --format='value(publicKey.pem)' > wrap_pub.pem
> openssl rand -out aes.key 32
> openssl enc -id-aes256-wrap-pad -K "$(xxd -p -c 64 < aes.key | tr -d '\n')" -iv a65959a6 \
>   -in target.der -out wrapped_target.bin
> openssl pkeyutl -encrypt -pubin -inkey wrap_pub.pem -pkeyopt rsa_padding_mode:oaep \
>   -pkeyopt rsa_oaep_md:sha256 -pkeyopt rsa_mgf1_md:sha256 -in aes.key -out enc_aes.bin
> cat enc_aes.bin wrapped_target.bin > wrapped_key.bin
> gcloud kms keys versions import --key="$KEY" --keyring="$KEYRING" --location="$REGION" \
>   --import-job="$JOB" --algorithm=rsa-sign-pkcs1-2048-sha256 --wrapped-key-file=wrapped_key.bin
> ```

Confirm the imported key really is the App's key by comparing public moduli — a mismatch here
produces JWTs GitHub silently rejects:

```bash
gcloud kms keys versions get-public-key <VERSION> --key="$KEY" --keyring="$KEYRING" \
  --location="$REGION" --output-file=kms_pub.pem
openssl rsa -pubin -in kms_pub.pem -noout -modulus | openssl sha256
openssl rsa -in app.pem -pubout | openssl rsa -pubin -noout -modulus | openssl sha256
```

Rotating the App (or moving to an org-owned one) means importing a new version. `provision_10`
always deploys the **highest ENABLED** version, so disable superseded ones to keep it obvious:

```bash
gcloud kms keys versions disable <OLD_VERSION> --key="$KEY" --keyring="$KEYRING" --location="$REGION"
```

## Manual Testing

To manually verify the Token Minter integration, you can execute a debug pod running in the same namespace as the agent.

1. Start an interactive debug pod containing `curl`:

```bash
kubectl run debug-box --rm -it \
  --image=curlimages/curl \
  --namespace=kubeagents-system \
  --labels="app=platform-agent" \
  --overrides='
  {
    "spec": {
      "serviceAccountName": "kubeagents-platform-agent"
    }
  }' -- sh
```

2. Once inside the pod, obtain the Google Service Account OIDC token using the metadata server. The `audience` parameter must reflect the URL of the Minty service.
3. Call the token minter using the retrieved token to request an installation access token.

```bash
# 1. Get the Google Service Account OIDC token
AUDIENCE="http://github-token-minter.kubeagents-system.svc.cluster.local:8080"
OIDC_TOKEN=$(curl -s -H "Metadata-Flavor: Google" "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity?audience=${AUDIENCE}&format=full")

# 2. Call the minter
curl -i -X POST http://github-token-minter.kubeagents-system.svc.cluster.local:8080/token \
  -H "Content-Type: application/json" \
  -H "X-OIDC-Token: $OIDC_TOKEN" \
  -d '{
    "org_name": "YOUR_GITHUB_ORG",
    "repositories": ["YOUR_GITHUB_REPO"],
    "scope": "platform-agent-scope"
  }'
```

If successful, Minty will return a JSON payload containing the short-lived GitHub access token.
