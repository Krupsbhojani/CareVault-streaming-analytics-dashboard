# Deploying the pipeline job to Azure

The CareVault pipeline (replayer + worker) runs as a scheduled **Azure
Container Apps job**. The job scales to zero between runs — billing is for
execution time only. Each run is 20 wall-minutes of replay; at speed factor 5
that advances source time by **100 source-minutes** (1 h 40 min of the
underlying AZT1D trace) per execution, into the Azure DB that already backs
the dashboard. (Event_time stamps in the DB advance at wall-clock pace, so
each run also lays down ~20 min of event_time, which is what the dashboard's
"last 60 min of event_time" window reads from.)

Dashboard App Service (`carevault-dash-kb`) is **not** touched by this
deployment. It just starts seeing data again because the pipeline writes
to the same database.

---

## 1. Cost comparison — registry choice

The job itself fits inside the Azure Container Apps free grant. The only
monthly fixed cost is the container registry we pull from.

**Container Apps compute** — free grant **per Azure subscription per month**
(shared across every Container Apps workload in the subscription — if you
also run a web app, another job, or an idle Container App in the same
subscription, their usage eats into the same pool before this pipeline sees
any over-grant charges):
- 180,000 vCPU-seconds
- 360,000 GiB-seconds
- 2,000,000 requests

At the recommended config (0.5 vCPU, 1 GiB RAM, 8 runs/day × 30 days,
~25 wall-min per run including prewarm + drain + safety margin):

| metric | monthly use | free grant | over? |
|---|---:|---:|---|
| vCPU-seconds | 8 × 30 × 1500 × 0.5 = **180,000** | 180,000 (shared) | at the limit |
| GiB-seconds | 8 × 30 × 1500 × 1.0 = **360,000** | 360,000 (shared) | at the limit |

→ Compute is effectively **$0/month** only if (a) the pipeline runs finish
on time AND (b) no other Container Apps workload in this subscription
consumes any of the grant. **~$0.50 to $5/month** if runs stretch or there's
sibling consumption (over-grant rates are ~$0.000024/vCPU-sec and
~$0.0000030/GiB-sec). Check your subscription's current Container Apps
usage before counting on free-tier compute.

**Registry options**, cheapest first:

| option | monthly | notes |
|---|---:|---|
| **GitHub Container Registry (`ghcr.io`), public image** | **$0.00** | Image carries no secrets (conninfo is injected at runtime). Simplest if the project repo is already on GitHub. Pulls from Container Apps work without auth for public images. |
| GitHub Container Registry, private image | ~$0.00 | GitHub free plan includes 500 MB of package storage; the pipeline image is ~350 MB. Requires a Container Apps secret with a GitHub PAT (`read:packages` scope only). |
| Docker Hub, public | ~$0.00 | Free, but anonymous-pull rate limits can bite on repeated job starts from the same egress IP range. Not recommended for scheduled production jobs. |
| ~~Azure Container Registry Basic~~ | ~~~$5.00~~ | **Not available on student / education subscriptions** — ACR Tasks (the server-side build service that `az acr build` uses) returns `TasksOperationsNotAllowed`. If you later move off a student sub, this becomes a one-command path again. |
| ACR Standard | ~$20 | Same constraint as Basic on student subs; also overkill. |

**Chosen path: `ghcr.io` public image, built in GitHub Actions.** Total
monthly bill for the pipeline deployment: **$0 – $2** depending on
run-time variance and sibling Container Apps consumption. The GitHub
Actions build itself is free on public repos and consumes free-tier
minutes on private repos (the pipeline build is ~2–3 min of a 2000 min/mo
free quota).

(The Azure Postgres B1ms the dashboard already uses is ~$13/mo and is
unchanged by this deployment.)

---

## 2. Schedule

Every 2 hours between 8am and midnight **America/New_York**. Nothing
between midnight and 8am NYT.

```
08:00  10:00  12:00  14:00  16:00  18:00  20:00  22:00   (NYT, 8 slots)
```

Container Apps cron is in **UTC**. During **DST (EDT, UTC-4)** —
in effect roughly March → first Sunday of November:

```
cron:  0 0,2,12,14,16,18,20,22 * * *      (UTC)
       = 20,22,08,10,12,14,16,18 NYT
```

During **standard time (EST, UTC-5)** — November → March:

```
cron:  0 1,3,13,15,17,19,21,23 * * *      (UTC)
       = 20,22,08,10,12,14,16,18 NYT
```

> ⚠️ **Container Apps cron does not respect DST.** The schedule in the
> `az` commands below is set for **EDT (UTC-4)**, correct through
> 2026-11-01. **It must be updated twice a year** — once in early March
> and once in early November — by running `az containerapp job update`
> with the other cron string above. Set a calendar reminder; nothing in
> the deployment auto-shifts.

The 8 slots per day × 30 days = **240 runs/month**.

---

## 3. Load

- **500 virtual patients** at **speed factor 5** (so ~100 source-min of
  replay per 20 wall-min run).
- At ~70 rows/sec sustained ingest on Azure B1ms, each run produces
  roughly **80,000 vitals_readings rows** plus alerts and predictions —
  more than enough to populate every dashboard panel including the
  forecast chart (which needs a 15-source-min horizon to resolve its
  first prediction, so ~3 min into each run the thin forecast lines
  start appearing on the person page).

- Deliberately **well below** the Azure saturation point measured in
  `docs/benchmark-azure.md` — the B1ms server hit CPU limits around
  20,000 rows/min; this workload is ~4,000 rows/min. The dashboard
  pulling in parallel is not a concern at this rate.

---

## 4. Files added for this deployment

(Nothing in `processor/`, `replay/`, or `dashboard/` was touched. The
existing `.github/workflows/main_carevault-dash-kb.yml` App Service workflow
is left alone — the new workflow added here has its own name, triggers, and
permission scope.)

- `docker/Dockerfile.pipeline` — pipeline image. `python:3.13-slim-bookworm`
  base, pinned deps, tini as PID 1. ~350 MB on disk after `pip install`.
- `docker/pipeline-entrypoint.sh` — orchestrates worker + replayer:
  seeds vps (idempotent), starts worker, waits for `prewarm_done` on the
  worker's startup log (not a fixed sleep), runs replayer for the
  configured duration, drains, SIGTERMs the worker, exits cleanly.
- `docker/pipeline-requirements.txt` — version-pinned Python deps,
  matched to the laptop versions used for the 30-min eval on 2026-10-05.
- `.dockerignore` — blacklist excluding `data/` (~900 MB), `scratch/`,
  `eval/`, `verification/`, `bench/`, `tests/`, `docs/`, `scripts/`,
  `sql/`, `dashboard/`, repo metadata, and generated artefacts. Measured
  context after: **~137 KiB across 13 files** (processor + replay + loader
  + the three pipeline files from docker/). Down from ~834 MB before.
- `.github/workflows/build-pipeline.yml` — GitHub Actions workflow that
  builds `docker/Dockerfile.pipeline` and pushes to
  `ghcr.io/<owner>/carevault-pipeline:{latest, sha-<short>, <full-sha>}`
  on every push to `main` that touches a pipeline-relevant path, and on
  manual `workflow_dispatch`. Auth is the built-in `GITHUB_TOKEN` with
  `packages: write` — no PAT, no secrets to rotate.

---

## 5. `az` commands — copy in order

**Prereqs** (one-time):
- `az login`
- `az account set --subscription <YOUR SUBSCRIPTION>`
- The project is pushed to a GitHub repo (private is fine). GitHub
  Actions builds the image for you; no local Docker, no PAT.

> ℹ️ **Note on `az acr build`:** on student / education Azure subscriptions
> ACR Tasks is disabled (the service returns `TasksOperationsNotAllowed`).
> That's why this deploy guide builds via GitHub Actions and pushes to
> `ghcr.io` instead of using ACR. If you later move to a subscription where
> ACR Tasks is allowed, the previous version of this doc in `git log` has
> the `az acr build` command.

### Variables (edit then export in your shell)

Paste the real values back to me at the "VALUES BACK TO CLAUDE" markers if
you want them captured in the repo docs; otherwise keep them local.

```bash
# —————————————————————————————————————————————————————————————
# Edit these three to match your account; the rest derive from them.
export RG="<existing-resource-group-containing-the-postgres>"            # VALUES BACK TO CLAUDE
export LOC="<region, e.g. northcentralus — same as the postgres server>" # VALUES BACK TO CLAUDE
export GH_OWNER="<github username or org that owns the repo, lowercase>" # VALUES BACK TO CLAUDE
# —————————————————————————————————————————————————————————————
export ENV_NAME="carevault-env"
export JOB_NAME="carevault-pipeline-job"
# Image tag: pick `latest` for "whatever is on main", or a specific
# `sha-<short>` to pin to a known-good build (what the workflow prints
# in its run summary).
export IMAGE_REF="ghcr.io/${GH_OWNER}/carevault-pipeline:latest"
export PG_HOST="carevault-pg-kb.postgres.database.azure.com"
# Conninfo strings — same shape as laptop; the password values are secrets.
# NEVER paste these back to me.
export PG_ADMIN_URL="host=${PG_HOST} port=5432 dbname=carevault user=cvadmin password=<CVADMIN_PASSWORD> sslmode=require"
export PG_PROC_URL="host=${PG_HOST} port=5432 dbname=carevault user=processor_role password=<PROCESSOR_PASSWORD> sslmode=require"
```

### Step 1 — make sure the Container Apps extension is installed

```bash
az extension add --name containerapp --upgrade
az provider register --namespace Microsoft.App
az provider register --namespace Microsoft.OperationalInsights
```

**Expected:** silence or "Extension already installed"; registration states
go to `Registered` within ~1 min. Verify:
```bash
az provider show -n Microsoft.App --query registrationState -o tsv   # → Registered
```

### Step 2 — create (or reuse) the Container Apps environment

```bash
az containerapp env create \
  --name "$ENV_NAME" \
  --resource-group "$RG" \
  --location "$LOC"
```

**Expected:** JSON block ending with `"provisioningState": "Succeeded"`.
If you already have an environment in this RG for other container apps,
reuse it instead — set `ENV_NAME` to the existing one and skip this
command.

### Step 3 — build the image via GitHub Actions, push to GHCR

The workflow is already in the repo (`.github/workflows/build-pipeline.yml`).
Push the changes under `.github/`, `docker/`, `.dockerignore`, `processor/`,
`replay/`, or `loader/` to `main` to trigger it, or run it manually.

**3a — commit + push (triggers the workflow via the `paths:` filter):**
```bash
# From the project root, on a clean checkout of main
git add .dockerignore docker/Dockerfile.pipeline docker/pipeline-entrypoint.sh \
        docker/pipeline-requirements.txt docker/pipeline-deploy.md \
        .github/workflows/build-pipeline.yml
git commit -m "deploy: pipeline image + GHCR build workflow"
git push origin main
```

**Expected:** the push lands on GitHub; within ~30 s a new run appears under
**Actions → "Build and push pipeline image to GHCR"**. Build + push takes
~2–3 minutes on a cold cache, ~1 minute warm. The run's summary page prints
the three tags published, e.g.
```
ghcr.io/<owner>/carevault-pipeline:latest
ghcr.io/<owner>/carevault-pipeline:sha-a1b2c3d
ghcr.io/<owner>/carevault-pipeline:a1b2c3d4e5f6…
```

**3b — manual trigger (same workflow, no commit required):**
```bash
# Needs the `gh` CLI; or do it in the Actions UI: Run workflow → main.
gh workflow run build-pipeline.yml --ref main
gh run watch                     # tails the latest run
```

**3c — set image visibility to public (recommended; one-time, via GitHub UI):**

Because the repo is private, the GHCR package it produces is **also private
by default**. For a scheduled Container Apps job you have two options:

| option | ongoing work | notes |
|---|---|---|
| **A — Make the GHCR package public.** GitHub → your profile/org → **Packages** → `carevault-pipeline` → **Package settings** → **Change visibility** → **Public**. | zero — pulls become anonymous, no secret in Container Apps | **Recommended.** The image contains no secrets (conninfo is injected at runtime), no proprietary logic that isn't already in the (private) repo. Everyone can read a 350 MB python wheel bundle; nobody can do anything with it without the DB passwords. |
| B — Keep the GHCR package private. | create a GitHub PAT with `read:packages` scope, store it as a Container Apps secret, pass it to the job via `--registry-password`, and rotate it when it expires. | Only pick this if there's a specific policy that says "no public packages" — e.g. the image contains proprietary data the repo visibility is also protecting. |

**Pick A.** The rest of this doc assumes the package is public. If you pick B,
see §5.1 at the end for the extra flags on `az containerapp job create`.

### Step 4 — create the scheduled job

Assumes you went with **3c Option A** (public GHCR package). The job's
`--image` points at `ghcr.io/<owner>/carevault-pipeline:latest`; Container
Apps pulls it anonymously, no `--registry-*` flags.

**EDT schedule (correct through 2026-11-01):**

```bash
az containerapp job create \
  --name "$JOB_NAME" \
  --resource-group "$RG" \
  --environment "$ENV_NAME" \
  --trigger-type Schedule \
  --cron-expression "0 0,2,12,14,16,18,20,22 * * *" \
  --replica-timeout 1800 \
  --replica-retry-limit 0 \
  --parallelism 1 \
  --replica-completion-count 1 \
  --cpu 0.5 --memory 1Gi \
  --image "$IMAGE_REF" \
  --secrets "pg-admin=${PG_ADMIN_URL}" "pg-proc=${PG_PROC_URL}" \
  --env-vars \
      "CAREVAULT_PG=secretref:pg-admin" \
      "CAREVAULT_PROCESSOR_PG=secretref:pg-proc" \
      "PIPELINE_VP_COUNT=500" \
      "PIPELINE_SPEED=5" \
      "PIPELINE_DURATION_SEC=1200"
```

**Expected:** JSON block ending in `"provisioningState": "Succeeded"` plus a
`"configuration": { "triggerType": "Schedule", ... }` section. The job
will not run yet — triggers only fire on the cron, or when you kick a
manual execution (step 5).

**Replica settings explained:**
- `--replica-timeout 1800` → 30 min hard cap. The pipeline aims for
  ~22-24 min (20 min replay + ~30 s prewarm + 90 s drain + overhead), so
  this leaves ~6 min of slack and still cuts off a runaway run before it
  burns a meaningful amount of money.
- `--replica-retry-limit 0` → if a run fails, don't retry; the next
  scheduled slot is only 2 h away.
- `--parallelism 1` + `--replica-completion-count 1` → one replica per
  execution, exits when the replica exits.

#### 5.1 — if you kept the GHCR package private (3c Option B)

Add these flags to the `az containerapp job create` command. The PAT needs
only `read:packages`; store it as a Container Apps secret so it isn't on
your shell history or in the job spec:

```bash
# PAT in a secret, then referenced from --registry-password via secretref.
export GH_READ_PACKAGES_PAT="<github PAT with read:packages>"

az containerapp job create \
  ... (all the flags above) ... \
  --secrets \
      "pg-admin=${PG_ADMIN_URL}" \
      "pg-proc=${PG_PROC_URL}" \
      "ghcr-pat=${GH_READ_PACKAGES_PAT}" \
  --registry-server "ghcr.io" \
  --registry-username "${GH_OWNER}" \
  --registry-password "secretref:ghcr-pat"
```

Rotate the PAT before it expires or the job will stop pulling.

### Step 5 — trigger a test execution immediately

```bash
az containerapp job start --name "$JOB_NAME" --resource-group "$RG"
```

**Expected:** JSON with `"name": "<job-name>-<execution-id>"` and
`"status": "Running"`.

Watch it:
```bash
# List executions (most recent first)
az containerapp job execution list \
  --name "$JOB_NAME" --resource-group "$RG" -o table

# Stream logs from the latest replica (replace <execution-id> from above)
az containerapp job logs show \
  --name "$JOB_NAME" --resource-group "$RG" \
  --container "$JOB_NAME" --follow --tail 200
```

**Expected first ~2 min of log:**
```
[entrypoint 12:00:03] config: vps=500 speed=5x duration=1200s
[entrypoint 12:00:03] seeding 500 virtual patients (seed=42, cohort=AZT1D)
[entrypoint 12:00:08] starting worker
[entrypoint 12:00:08] worker pid=14
[entrypoint 12:00:08] waiting for worker prewarm (timeout=300s)
[worker 0/1] starting
...
[entrypoint 12:00:38] worker ready after 30s
[entrypoint 12:00:38] starting replayer for 1200s
```

Then at ~t+20min, replayer finishes, drain runs, worker exits, container exits.

### Step 6 — verify data landed

```bash
# From your laptop (or any machine with psql / python):
# Should return a recent event_time and a non-zero fresh-row count.
```

```sql
-- Replace the password placeholder.
-- psql "host=carevault-pg-kb.postgres.database.azure.com port=5432 dbname=carevault user=cvadmin sslmode=require"
SELECT max(event_time)                       AS latest_event_time,
       count(*)                              AS rows_last_hour,
       count(DISTINCT virtual_patient_id)    AS vps_active
  FROM live.vitals_readings
 WHERE ingested_at > now() - interval '1 hour';
```

**Expected after a successful run:** `latest_event_time` within the last
few minutes, `rows_last_hour` on the order of 80,000, `vps_active`
around 500.

### Step 7 — DST reminders

Set a calendar reminder for:
- **2026-11-01 ~2am ET** — DST ends. Update to EST cron:
  ```bash
  az containerapp job update --name "$JOB_NAME" --resource-group "$RG" \
    --cron-expression "0 1,3,13,15,17,19,21,23 * * *"
  ```
- **2027-03-14 ~2am ET** — DST starts. Revert to EDT cron (step 4's value).

---

## 6. Common operations

**Change the schedule or vp count:**
```bash
az containerapp job update --name "$JOB_NAME" --resource-group "$RG" \
  --cron-expression "<new-cron>" \
  --set-env-vars "PIPELINE_VP_COUNT=<n>" "PIPELINE_SPEED=<x>"
```

**Rebuild + redeploy:**

A `git push` to `main` that changes anything under `processor/`, `replay/`,
`loader/`, `docker/`, or `.dockerignore` triggers the GHCR workflow
automatically and republishes `:latest`. Since the job is configured with
`--image ...:latest`, the next scheduled execution picks it up.

To force the next run to use a *specific* build (e.g. you want to pin to a
reviewed SHA, not whatever is latest on main):
```bash
# From the Actions run summary, grab the sha-<short> tag it published.
az containerapp job update --name "$JOB_NAME" --resource-group "$RG" \
  --image "ghcr.io/${GH_OWNER}/carevault-pipeline:sha-<short-sha>"
```

**Pause the job (no runs until re-enabled):**

Cron ORs day-of-month with day-of-week when both are set to anything other
than `*`, so `0 0 1 1 0` actually fires every Sunday as well as Jan 1 — not
a pause at all. Two reliable options:

**Option A — delete the job (recommended; also stops any accidental
state drift):**
```bash
az containerapp job delete --name "$JOB_NAME" --resource-group "$RG" --yes
# To resume, re-run step 4 (the full `az containerapp job create ...`)
# with the same name and image.
```

**Option B — set a cron that is syntactically valid but can never fire,
leaving the job definition intact:**
```bash
az containerapp job update --name "$JOB_NAME" --resource-group "$RG" \
  --cron-expression "0 0 31 2 *"   # 00:00 on Feb 31 — Feb never has 31 days
# To resume, re-run `az containerapp job update ... --cron-expression ...`
# with the real cron from step 4.
```
Day-of-week stays `*` here so there's no OR trap. Both fields are inside
their valid ranges (so Azure accepts the string), but the date combination
is unreachable, so the trigger never fires.

**Tear everything down:**
```bash
az containerapp job delete --name "$JOB_NAME" --resource-group "$RG" --yes
az containerapp env delete --name "$ENV_NAME" --resource-group "$RG" --yes  # only if nothing else uses it
# plus: delete the GHCR package via GitHub UI → Packages → carevault-pipeline → Delete.
```
