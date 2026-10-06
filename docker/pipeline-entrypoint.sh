#!/usr/bin/env bash
# CareVault — pipeline entrypoint for Container Apps scheduled jobs.
#
# Runs the worker + replayer together inside one container for a fixed
# duration, then exits so the job replica terminates and Container Apps
# stops billing. The container is scaled to zero between runs.
#
# Sequence:
#   1. Seed the 500 virtual patients (idempotent upsert; no --reset so we
#      don't wipe worker_state and lose per-vp dedup continuity).
#   2. Start the stream worker in the background; capture its PID.
#   3. Watch its startup log for "prewarm_done" before releasing the
#      replayer — the worker runs one SELECT per vp during prewarm (~30 s
#      at 500 vps on Azure), and starting the replayer before prewarm
#      means the first wave of new readings sits in a 10-s overlap window
#      the worker hasn't entered yet. Detect readiness, don't sleep a
#      fixed guess (the user's instruction).
#   4. Run the replayer in the foreground for PIPELINE_DURATION_SEC.
#   5. Let the worker drain its backlog (PIPELINE_DRAIN_SEC).
#   6. SIGTERM the worker, wait for it to flush, SIGKILL if it hangs.
#   7. Exit with the replayer's exit code.
#
# Required env (set via Container Apps secrets, never baked in):
#   CAREVAULT_PG            — admin conninfo (build_vps, replay/run)
#   CAREVAULT_PROCESSOR_PG  — processor_role conninfo (worker)
#
# Optional env (sensible defaults; override via Container Apps env):
#   PIPELINE_VP_COUNT       — virtual patient count          (default 500)
#   PIPELINE_SPEED          — replay speed factor            (default 5)
#   PIPELINE_DURATION_SEC   — replayer run time              (default 1200 = 20 min)
#   PIPELINE_PREWARM_TIMEOUT_SEC — max wait for prewarm_done (default 300)
#   PIPELINE_DRAIN_SEC      — wait between replay end + worker TERM (default 90)
#   PIPELINE_SHUTDOWN_SEC   — max wait for worker to exit on TERM   (default 60)

set -euo pipefail

log() { printf '[entrypoint %s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }
die() { log "FATAL: $*"; exit 1; }

: "${CAREVAULT_PG:?CAREVAULT_PG not set}"
: "${CAREVAULT_PROCESSOR_PG:?CAREVAULT_PROCESSOR_PG not set}"

VP_COUNT="${PIPELINE_VP_COUNT:-500}"
SPEED="${PIPELINE_SPEED:-5}"
DURATION_SEC="${PIPELINE_DURATION_SEC:-1200}"
PREWARM_TIMEOUT_SEC="${PIPELINE_PREWARM_TIMEOUT_SEC:-300}"
DRAIN_SEC="${PIPELINE_DRAIN_SEC:-90}"
SHUTDOWN_SEC="${PIPELINE_SHUTDOWN_SEC:-60}"

STARTUP_LOG="/tmp/worker_startup.log"
rm -f "$STARTUP_LOG"
export PROCESSOR_STARTUP_LOG="$STARTUP_LOG"
export PYTHONPATH="/app"

log "config: vps=$VP_COUNT speed=${SPEED}x duration=${DURATION_SEC}s"
log "config: prewarm_timeout=${PREWARM_TIMEOUT_SEC}s drain=${DRAIN_SEC}s shutdown=${SHUTDOWN_SEC}s"

# ---- step 1: seed vps ------------------------------------------------------
# No --reset: idempotent upsert via ON CONFLICT in build_vps. Keeps
# live.worker_state and existing live.vitals_readings intact across runs.
log "seeding $VP_COUNT virtual patients (seed=42, cohort=AZT1D)"
python -u replay/build_vps.py \
    --count "$VP_COUNT" --seed 42 --speed "$SPEED" --source-cohort AZT1D \
    || die "build_vps failed"

# ---- step 2: start worker --------------------------------------------------
log "starting worker"
python -u processor/worker.py --shard 0 --num-shards 1 &
WORKER_PID=$!
log "worker pid=$WORKER_PID"

# Ensure the worker gets stopped even if the script dies unexpectedly.
cleanup_worker() {
    if kill -0 "$WORKER_PID" 2>/dev/null; then
        log "cleanup: TERM worker pid=$WORKER_PID"
        kill -TERM "$WORKER_PID" 2>/dev/null || true
        # Grace period, then KILL.
        for _ in $(seq 1 "$SHUTDOWN_SEC"); do
            kill -0 "$WORKER_PID" 2>/dev/null || break
            sleep 1
        done
        if kill -0 "$WORKER_PID" 2>/dev/null; then
            log "cleanup: worker didn't exit on TERM, sending KILL"
            kill -KILL "$WORKER_PID" 2>/dev/null || true
        fi
        wait "$WORKER_PID" 2>/dev/null || true
    fi
}
trap cleanup_worker EXIT

# ---- step 3: wait for prewarm_done ----------------------------------------
# worker.py writes a line "<perf_counter>\tprewarm_done" to
# PROCESSOR_STARTUP_LOG after the per-vp MAD-window backfill finishes.
# At 500 vps on Azure that's ~30 s; we give it up to PREWARM_TIMEOUT_SEC.
log "waiting for worker prewarm (timeout=${PREWARM_TIMEOUT_SEC}s)"
PREWARM_START=$SECONDS
while true; do
    if [[ -f "$STARTUP_LOG" ]] && grep -q 'prewarm_done' "$STARTUP_LOG"; then
        log "worker ready after $((SECONDS - PREWARM_START))s"
        break
    fi
    if ! kill -0 "$WORKER_PID" 2>/dev/null; then
        die "worker exited before prewarm finished; last startup log:
$(cat "$STARTUP_LOG" 2>/dev/null || echo '(startup log empty)')"
    fi
    if (( SECONDS - PREWARM_START > PREWARM_TIMEOUT_SEC )); then
        die "prewarm did not complete in ${PREWARM_TIMEOUT_SEC}s; last log:
$(cat "$STARTUP_LOG" 2>/dev/null || echo '(startup log empty)')"
    fi
    sleep 2
done

# ---- step 4: run replayer in foreground -----------------------------------
log "starting replayer for ${DURATION_SEC}s"
REPLAY_EXIT=0
python -u replay/run.py \
    --vp-count "$VP_COUNT" --speed "$SPEED" --duration "$DURATION_SEC" \
    || REPLAY_EXIT=$?
log "replayer exited with code=$REPLAY_EXIT"

# ---- step 5: drain -------------------------------------------------------
# Replayer stops producing; the worker's last fetch cycle still has to
# commit alerts + resolve pending predictions. DRAIN_SEC covers that.
log "draining worker for ${DRAIN_SEC}s"
sleep "$DRAIN_SEC"

# ---- step 6: shut worker down cleanly ------------------------------------
# Trap handler does the work; we just trigger it.
log "stopping worker"
cleanup_worker
trap - EXIT

log "done; exit=$REPLAY_EXIT"
exit "$REPLAY_EXIT"
