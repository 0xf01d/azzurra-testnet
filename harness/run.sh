#!/bin/sh
# Synthetic-corpus run driver — one (seed, scale) pair end-to-end:
#
#   harness/run.sh SEED [USERS] [CHANNELS]
#
# 1. brings the stack up (harness overlay included — SVC_AKILL_CLONES=0),
# 2. runs the generator (real commands, journaling everything),
# 3. stops services GRACEFULLY (SIGTERM → Epona saves the flatfiles) and
#    copies them out of the container,
# 4. archives journal + passwords + manifest + flatfiles into
#    harness/out/seed-<SEED>.tar.zst.
#
# Fully automated: this is the unit GitHub Actions calls, ×10 seeds.
set -eu

SEED="${1:?usage: run.sh SEED [USERS] [CHANNELS]}"
USERS="${2:-1000}"
CHANNELS="${3:-1000}"

cd "$(dirname "$0")/.."
OUT="harness/out/seed-$SEED"
DB="$OUT/db"
COMPOSE="docker compose -f compose.yaml -f harness/compose.harness.yaml"
mkdir -p "$OUT"

echo "=== stack fresh (seed $SEED, $USERS users / $CHANNELS channels) ==="
# -v: wipe services-data so every run starts from an EMPTY database —
# stale registered nicks/channels would poison the corpus (CS REGISTER
# 'already registered'), and each seed must be an independent migration DB.
$COMPOSE down -v --remove-orphans >/dev/null 2>&1 || true
$COMPOSE up -d --wait

echo "=== driving workload ==="
$COMPOSE run --rm corpus --seed "$SEED" --users "$USERS" \
    --channels "$CHANNELS" --out "/out/seed-$SEED"

echo "=== graceful services stop (SIGINT = save databases, then quit) ==="
# azzurra/services: SIGTERM quits WITHOUT saving (signals.c:174); SIGINT
# saves the flatfiles then quits (signals.c:162) — same as /rs SHUTDOWN.
$COMPOSE kill -s INT services
sleep 5
$COMPOSE stop services || true
mkdir -p "$DB"
docker cp azzurra-services:/opt/azzurra/services/data/. "$DB/"

echo "=== archiving ==="
tar --zstd -cf "harness/out/seed-$SEED.tar.zst" -C "$OUT" \
    journal.ndjson passwords.json manifest.json db
echo "=== done: harness/out/seed-$SEED.tar.zst ==="
