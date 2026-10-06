# Deploying the CareVault pipeline

The pipeline (replayer + worker) runs as a **scheduled GitHub Actions
workflow** that runs the Python directly on an `ubuntu-latest` runner,
not inside a container. The workflow is `.github/workflows/run-pipeline.yml`.

Dashboard App Service (`carevault-dash-kb`) is **not** touched by this
deployment. It just starts seeing data because the pipeline writes to the
Azure PostgreSQL it already reads from.

---

## 1. Why not Azure (and why not a container)

The project's Azure subscription is **Azure for Students**. Two
restrictions on that tier made the container path unworkable:

| what we tried | how it failed on Students |
|---|---|
| `az acr build` to produce the image | ACR Tasks is disabled on Students (`TasksOperationsNotAllowed`). |
| Azure Container Apps **jobs** to run the image on a cron | Container Apps on Students is forced to the **Express managed environment**, which only supports Container Apps (long-running web services), not **jobs**. Jobs require a Consumption environment that the subscription will not provision. |

Switching to GitHub Actions sidesteps both. It also eliminates the Container
Registry entirely — the runner clones the repo and runs Python directly.

**The pipeline Dockerfile and entrypoint are intentionally kept in `docker/`**
as a record of the attempted container path. They are **not invoked** by the
current deployment; see section 7.

---

## 2. Budget

GitHub Actions free quota on a **private** repo: **2,000 minutes/month**,
shared across every workflow in the repo.

| workflow | runs | minutes/run | monthly use |
|---|---|---:|---:|
| **`run-pipeline.yml`** (this deployment) | 3/day × 30 | ~17 (setup + replay + drain) | **~1,530** |
| `build-pipeline.yml` (GHCR image build — currently dead code; see §7) | on push to pipeline paths | ~2–3 | ~15–30 |
| `main_carevault-dash-kb.yml` (dashboard App Service deploy) | on push | ~3–4 | ~30–60 |
| **total** | | | **~1,580–1,620** |

Headroom in the 2,000-min quota: **~380–420 min**. Enough for manual
`workflow_dispatch` re-runs before demos. If the budget tightens, cut a
scheduled run slot or reduce `PIPELINE_DURATION_SEC`.

Postgres B1ms Flexible Server is ~$13/mo and is unchanged by this
deployment. No other Azure costs are incurred — nothing is deployed to
Azure for this pipeline.

---

## 3. Schedule

Three runs per day, spread across New York working hours: **9am, 1pm, 5pm
America/New_York**. Nothing between 5pm and 9am NYT; nothing on weekends
is **not** currently excluded (cron fires 7 days/week).

**GitHub Actions cron is UTC and does NOT shift for daylight-saving.** The
workflow file ships with the EDT cron active and the EST cron commented
out. Update twice a year:

| regime | NYC slots | cron (UTC) |
|---|---|---|
| **EDT (UTC-4)** — in effect now through 2026-11-01 | 09:00, 13:00, 17:00 | `0 13,17,21 * * *` |
| **EST (UTC-5)** | 09:00, 13:00, 17:00 | `0 14,18,22 * * *` |

Reminders:
- **2026-11-01** — DST ends. In `.github/workflows/run-pipeline.yml`,
  comment out the EDT cron line and uncomment the EST one. Commit, push.
- **2027-03-14** — DST starts. Revert.

---

## 4. Load

- **500 virtual patients** at **speed factor 5** → 20 wall-min of replay
  advances source time by 100 source-min; this workflow runs 15 wall-min
  = **75 source-minutes of patient data** per execution.
- Each run produces roughly **60,000 vitals_readings rows** plus alerts
  and predictions — enough for every dashboard panel including the
  forecast chart (which needs a 15-source-min horizon to resolve its first
  prediction, so ~3 wall-min into each run the thin forecast lines on the
  person page start appearing).
- Well below the Azure B1ms saturation point measured in
  `docs/benchmark-azure.md` (~20,000 rows/min CPU limit); this workload is
  ~4,000 rows/min.

---

## 5. Files added for this deployment

- **`.github/workflows/run-pipeline.yml`** — the scheduled workflow.
  Cron + `workflow_dispatch`; concurrency group `run-pipeline` so two runs
  never overlap; `timeout-minutes: 22`; secrets `CAREVAULT_PG` and
  `CAREVAULT_PROCESSOR_PG` injected via `env:` into the one step that runs
  Python. Reuses the exact same prewarm-detect / drain / signal shutdown
  logic as `docker/pipeline-entrypoint.sh`, inlined so the workflow is
  self-contained.
- **`docker/pipeline-requirements.txt`** — version-pinned Python deps,
  matched to the laptop versions used for the 30-min eval on 2026-10-05.
  Reused by this workflow via `actions/setup-python@v5`'s pip cache.

Unchanged / untouched:
- `processor/`, `replay/`, `loader/`, `dashboard/` and the database schema.
- `.github/workflows/main_carevault-dash-kb.yml` (dashboard deploy).
- `.github/workflows/build-pipeline.yml` (GHCR image build, now dead code).

---

## 6. What you need to do

### 6.1 Add the two repository secrets

GitHub UI path:

1. Open the repo on github.com.
2. **Settings** (top navigation of the repo, requires admin).
3. Left sidebar: **Secrets and variables → Actions**.
4. Click **New repository secret**.
5. Add each of the two below. Paste the full conninfo including the password.

| Name | Value |
|---|---|
| `CAREVAULT_PG` | `host=carevault-pg-kb.postgres.database.azure.com port=5432 dbname=carevault user=cvadmin password=<CVADMIN_PASSWORD> sslmode=require` |
| `CAREVAULT_PROCESSOR_PG` | `host=carevault-pg-kb.postgres.database.azure.com port=5432 dbname=carevault user=processor_role password=<PROCESSOR_PASSWORD> sslmode=require` |

Both are **Repository secrets** (not environment secrets). GitHub Actions
masks these values in any log line that would print them literally. The
workflow never echoes them.

### 6.2 Trigger a test run immediately

Either UI path or CLI, both work:

**UI:**
1. **Actions** tab of the repo.
2. Left sidebar: **"Run CareVault pipeline (scheduled, runner-direct)"**.
3. **Run workflow** button (top right of the run list) → ref `main` → **Run workflow**.
4. The run appears within ~5 s. Click into it to tail the log.

**CLI (needs `gh` logged in):**
```bash
gh workflow run run-pipeline.yml --ref main
gh run watch                             # tails the newest run
```

Expected log opening:
```
[run 13:01:02] live.virtual_patient rows found: 500      # or 0 on first run → seed line follows
[run 13:01:02] skipping seed (table already populated)
[run 13:01:02] starting worker
[run 13:01:02] worker pid=1823
[run 13:01:02] waiting for worker prewarm (timeout=300s)
...
[run 13:01:33] worker ready after 31s
[run 13:01:33] starting replayer for 900s
...
```

Then 15 min of replay, 60 s drain, 10 s shutdown, step exits. The next
step (`Verification`) prints `latest_event_time`, `rows_last_30m`,
`vps_total`, and alerts broken down by detector.

### 6.3 Verify after the run

The workflow's **Verification** step runs `SELECT max(event_time), …` from
within the runner (reuses `CAREVAULT_PG`). Alternatively, from your
laptop:

```sql
-- psql "host=carevault-pg-kb.postgres.database.azure.com port=5432 dbname=carevault user=cvadmin sslmode=require"
SELECT max(event_time)                       AS latest_event_time,
       count(*) FILTER (WHERE ingested_at > now() - interval '30 minutes') AS rows_last_30m,
       count(DISTINCT virtual_patient_id)    AS vps_total
  FROM live.vitals_readings;
```

Expected after a successful run: `latest_event_time` within the last few
minutes, `rows_last_30m` on the order of 60,000, `vps_total` ≥ 500.

The dashboard at `carevault-dash-kb` picks the data up on its next refresh.

---

## 7. Network / firewall

The GitHub-hosted `ubuntu-latest` runner connects to Azure PostgreSQL **over
the public internet** on port 5432. GitHub Actions runners have egress from
a wide, ephemeral range of IPs (documented in GitHub's meta API but not
practical to allow-list by range).

The Postgres server's firewall is currently configured to **allow all IPs**
(`0.0.0.0/0`). **Tightening that rule later would break this workflow** —
if the pipeline suddenly stops writing after a firewall edit, this is why.
Mitigations in rough preference order:

1. Keep "allow all IPs" and rely on SSL + strong role passwords for
   security. The processor_role has column-level grants that explicitly
   exclude `injected_type`; the admin role is only used for the one-time
   vp seed. Current setup.
2. Deploy a self-hosted GHA runner inside the Azure VNet and allow-list
   its single IP. Costs an Azure VM but tightens firewall.
3. Move the pipeline back onto Azure (requires a non-Student
   subscription — see §1).

---

## 8. Files kept as a record (unused)

Both files below were produced for the Azure Container Apps path. They
remain in `docker/` because the implementation is sound and would work on
any non-Student Azure subscription — if the project moves off the Student
tier, you can revive the ACA deployment without rebuilding from scratch.
The current workflow does **not** invoke either file.

| file | status | why kept |
|---|---|---|
| `docker/Dockerfile.pipeline` | **unused** | reproducible image recipe + pinned deps. If an ACA deployment becomes possible, build via `az acr build` (or GitHub Actions `build-pipeline.yml` → GHCR) + create the ACA job. |
| `docker/pipeline-entrypoint.sh` | **unused** | same prewarm-detect + drain + signal-shutdown logic the GHA workflow now inlines. Preserved so a future ACA deployment can reuse it as the container entrypoint. |
| `docker/pipeline-requirements.txt` | **used by the GHA workflow** | installed via `actions/setup-python@v5` with pip cache. Also the source of truth for the Dockerfile's `pip install` step. |
| `.github/workflows/build-pipeline.yml` | **dead code** (triggers only on changes to pipeline files, publishes to GHCR, which nothing pulls from) | kept because it still produces a valid, pullable image if someone ever needs one. Costs ~15 min/month of Actions quota. If you want to stop it entirely, replace its `on:` with `on: workflow_dispatch:` so it only runs when invoked manually. |

---

## 9. Common operations

**Change the schedule or vp count:** edit `.github/workflows/run-pipeline.yml`
(the `cron:` lines in `on.schedule` and/or the `env:` block in the "Run
worker + replayer" step), commit, push. The next scheduled run picks up
the new values; in-flight runs are unaffected.

**Pause the pipeline (no runs until re-enabled):** two options.

- **A — turn the workflow off in the UI:** Actions → "Run CareVault
  pipeline (scheduled, runner-direct)" → **⋯** menu → **Disable workflow**.
  Scheduled triggers and `workflow_dispatch` both stop. Re-enable from the
  same menu. **Recommended** — no file churn.
- **B — comment out the `schedule:` lines** in the workflow file and push.
  `workflow_dispatch` still works for manual runs.

**Shift for DST:** see §3. One-line edit, twice a year.

**Rotate a secret:** Settings → Secrets and variables → Actions → click
the secret → **Update**. In-flight runs keep the old value; next run picks
up the new one.

**Tear down:**
```bash
# From the project root
git rm .github/workflows/run-pipeline.yml
git commit -m "deploy: tear down scheduled pipeline"
git push
```
The database and dashboard App Service are untouched.
