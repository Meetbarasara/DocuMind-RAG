# Deploying to Azure Container Apps (always-on) with GitHub Actions CI/CD

This gets the KYC Compliance app live on Azure as two managed containers with
free HTTPS, and wires GitHub Actions so every green push to `master` redeploys
automatically.

```
                 push to master ─► GitHub Actions (lint+test gate) ─► az acr build
                                                                          │
        ┌──────────────── Azure Container Registry (your images) ◄────────┘
        │                                   │
        ▼                                   ▼
  documind-api  (FastAPI, 2 vCPU / 4 GB) ── documind-frontend (Next.js, 0.5 vCPU / 1 GB)
  https://…azurecontainerapps.io           https://…azurecontainerapps.io
        │                                   │
        └── Supabase · Pinecone · Groq · Cerebras · Cohere (your existing accounts)
```

**Run every command below in [Azure Cloud Shell](https://shell.azure.com) (Bash).**
It's in your browser, has `az` pre-installed and already logged in — no local
install and none of Windows PowerShell's quoting quirks. (Git Bash + `az login`
works too.)

---

## Step 0 — Fill in these once, at the top of your Cloud Shell session

```bash
# --- edit these four ---
export SUB="$(az account show --query id -o tsv)"   # your default subscription; or paste an id
export RG="documind-rg"
export LOCATION="centralindia"                       # or eastus, westeurope, …
export ACR="documindacr$RANDOM"                      # must be globally unique, lowercase, no dashes

# --- fixed names (used by the CI pipeline too) ---
export ACA_ENV="documind-env"
export API_APP="documind-api"
export FRONTEND_APP="documind-frontend"
echo "ACR=$ACR  RG=$RG  SUB=$SUB"        # note the ACR name — you'll need it for GitHub later
```

You also need your service keys handy (the same values as your local `.env`):
`GROQ_API_KEY`, `PINECONE_API_KEY`, `PINECONE_INDEX_NAME`, `SUPABASE_URL`,
`SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`, `CEREBRAS_API_KEY`, and
optionally `COHERE_API_KEY`.

---

## Step 1 — Get the code into Cloud Shell

```bash
git clone https://github.com/Meetbarasara/DocuMind-RAG.git
cd DocuMind-RAG
```

---

## Step 2 — Create the resource group, registry, and Container Apps environment

```bash
az group create -n "$RG" -l "$LOCATION"

az acr create -n "$ACR" -g "$RG" --sku Basic
az acr update -n "$ACR" --admin-enabled true          # lets the apps pull images
export ACR_USER="$(az acr credential show -n "$ACR" --query username -o tsv)"
export ACR_PASS="$(az acr credential show -n "$ACR" --query 'passwords[0].value' -o tsv)"

az extension add --name containerapp --upgrade -y
az provider register --namespace Microsoft.App --wait
az containerapp env create -n "$ACA_ENV" -g "$RG" -l "$LOCATION"
```

---

## Step 3 — Build the api image (in the cloud) and create the api app

`az acr build` compiles the Docker image inside Azure — no local Docker needed.
This step also bakes the embedding model into the image (~5–8 min the first time).

```bash
az acr build -r "$ACR" -t documind-api:latest -f Dockerfile .
```

Create the always-on api app. **Paste your real key values** where shown:

```bash
az containerapp create \
  -n "$API_APP" -g "$RG" --environment "$ACA_ENV" \
  --image "$ACR.azurecr.io/documind-api:latest" \
  --registry-server "$ACR.azurecr.io" \
  --registry-username "$ACR_USER" --registry-password "$ACR_PASS" \
  --target-port 8000 --ingress external \
  --cpu 2 --memory 4Gi --min-replicas 1 --max-replicas 1 \
  --secrets \
     groq-key="<YOUR_GROQ_API_KEY>" \
     pinecone-key="<YOUR_PINECONE_API_KEY>" \
     supabase-url="<YOUR_SUPABASE_URL>" \
     supabase-anon="<YOUR_SUPABASE_ANON_KEY>" \
     supabase-service="<YOUR_SUPABASE_SERVICE_ROLE_KEY>" \
     cerebras-key="<YOUR_CEREBRAS_API_KEY>" \
     cohere-key="<YOUR_COHERE_API_KEY_or_leave_blank>" \
  --env-vars \
     GROQ_API_KEY=secretref:groq-key \
     PINECONE_API_KEY=secretref:pinecone-key \
     PINECONE_INDEX_NAME="<YOUR_PINECONE_INDEX_NAME>" \
     SUPABASE_URL=secretref:supabase-url \
     SUPABASE_ANON_KEY=secretref:supabase-anon \
     SUPABASE_SERVICE_ROLE_KEY=secretref:supabase-service \
     CEREBRAS_API_KEY=secretref:cerebras-key \
     COHERE_API_KEY=secretref:cohere-key \
     CORS_ORIGIN_REGEX='^https://.*\.azurecontainerapps\.io$'
```

> `CORS_ORIGIN_REGEX` matches any `*.azurecontainerapps.io` origin, so the api
> accepts the frontend without you having to know its URL in advance. (For a
> custom domain later, add it to `CORS_ORIGINS`.)

Grab the api's public URL — the frontend build needs it:

```bash
export API_FQDN="$(az containerapp show -n "$API_APP" -g "$RG" --query properties.configuration.ingress.fqdn -o tsv)"
echo "API is at https://$API_FQDN"
curl -fsS "https://$API_FQDN/health" && echo "  ← api healthy"
```

---

## Step 4 — Build the frontend (with the api URL baked in) and create the app

```bash
az acr build -r "$ACR" -t documind-frontend:latest \
  --build-arg "NEXT_PUBLIC_API_BASE=https://$API_FQDN" \
  ./frontend-next

az containerapp create \
  -n "$FRONTEND_APP" -g "$RG" --environment "$ACA_ENV" \
  --image "$ACR.azurecr.io/documind-frontend:latest" \
  --registry-server "$ACR.azurecr.io" \
  --registry-username "$ACR_USER" --registry-password "$ACR_PASS" \
  --target-port 3000 --ingress external \
  --cpu 0.5 --memory 1Gi --min-replicas 1 --max-replicas 1

export FRONTEND_FQDN="$(az containerapp show -n "$FRONTEND_APP" -g "$RG" --query properties.configuration.ingress.fqdn -o tsv)"
echo "────────────────────────────────────────────────"
echo "  Your app is live:  https://$FRONTEND_FQDN"
echo "  API:               https://$API_FQDN"
echo "────────────────────────────────────────────────"
```

Open the frontend URL — the demo gap table should render immediately (no login).
That already proves the frontend image + baked API URL are correct.

---

## Step 5 — Seed a regulation so live checks have something to run against

From Cloud Shell (or your local machine), the seed script writes to your shared
Supabase/Pinecone — run it once against a real circular PDF. If you don't have
one handy, the repo's synthetic fixtures work:

```bash
pip install -e . -q
python -m scripts.seed_regulation \
  --pdf data/compliance/rbi_kyc_requirements.pdf \
  --name "RBI KYC (demo)"
```

(You've already seeded two regulations on this Supabase project, so you can skip
this unless you want a fresh one.)

---

## Step 6 — Wire GitHub Actions so future pushes auto-deploy (OIDC, passwordless)

This lets the `deploy` job in `.github/workflows/ci.yml` redeploy on every green
push to `master` — no stored passwords.

```bash
# 1. An app registration GitHub will log in AS
az ad app create --display-name "documind-github-oidc"
export APP_ID="$(az ad app list --display-name documind-github-oidc --query '[0].appId' -o tsv)"
az ad sp create --id "$APP_ID"

# 2. Trust GitHub Actions on master (passwordless federated credential)
az ad app federated-credential create --id "$APP_ID" --parameters '{
  "name": "github-master",
  "issuer": "https://token.actions.githubusercontent.com",
  "subject": "repo:Meetbarasara/DocuMind-RAG:ref:refs/heads/master",
  "audiences": ["api://AzureADTokenExchange"]
}'

# 3. Let it manage this resource group only
az role assignment create --assignee "$APP_ID" --role Contributor \
  --scope "/subscriptions/$SUB/resourceGroups/$RG"

export TENANT="$(az account show --query tenantId -o tsv)"
echo "AZURE_CLIENT_ID       = $APP_ID"
echo "AZURE_TENANT_ID       = $TENANT"
echo "AZURE_SUBSCRIPTION_ID = $SUB"
echo "AZURE_ACR_NAME        = $ACR"
echo "AZURE_RESOURCE_GROUP  = $RG"
```

Then in **GitHub → your repo → Settings**:

- **Secrets and variables → Actions → Secrets** — add:
  `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID` (values printed above)
- **…→ Variables** — add:
  `AZURE_RESOURCE_GROUP=documind-rg`, `AZURE_ACR_NAME=<your ACR>`,
  `AZURE_API_APP=documind-api`, `AZURE_FRONTEND_APP=documind-frontend`

Now push anything to `master`: CI runs lint + 282 tests, and if green, the
`deploy` job rebuilds both images and updates both apps. (Until the variables
are set, the deploy job simply skips — it never fails the build.)

---

## Verifying a deploy

1. `https://<api>/health` → `{"status":"ok"}`
2. Frontend URL renders the demo gap table on load.
3. Sign in → upload a policy → pick a regulation → **Run check** streams a cited
   gap table (proves api + Cerebras + Pinecone + Supabase are all reachable).

## Cost & control

Always-on (`--min-replicas 1`) keeps both apps warm — instant, no cold starts.
Rough cost is **~$15–45/month**; the Azure free-trial **$200 credit covers the
first ~30 days**. To watch/limit spend:

- **Pause spend anytime:** `az containerapp update -n documind-api -g documind-rg --min-replicas 0` (and the frontend) — they scale to zero and stop billing compute until the next request (which then cold-starts). Set back to `1` to go warm again.
- **Tear it all down:** `az group delete -n documind-rg --yes` removes everything (apps, registry, env) in one go.
- Set a **budget alert**: Azure Portal → Cost Management → Budgets.

## If something fails

- **api container won't start / restarts** — check logs:
  `az containerapp logs show -n documind-api -g documind-rg --follow`.
  A missing required secret fails fast with a clear `pydantic` validation error.
- **Frontend loads but every action fails** — the api URL baked at build time is
  wrong, or CORS. Confirm `NEXT_PUBLIC_API_BASE` matched the api FQDN at build,
  and that `CORS_ORIGIN_REGEX` is set on the api.
- **Out-of-memory on the api** — bump it: `az containerapp update -n documind-api -g documind-rg --cpu 2 --memory 4Gi` (already the default here).
