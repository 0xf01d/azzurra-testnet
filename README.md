# Azzurra testnet infra

Dockerized Azzurra test network: **hub + leaf-v4 + leaf-v6 + services**, via `docker compose up`. Dev and testnet only. **Not** a production install path — for that see `INSTALL.md` in [azzurra/bahamut](https://github.com/azzurra/bahamut) and [azzurra/services](https://github.com/azzurra/services).

## Topology

```
                       ┌─────────────────────────┐
                       │   services              │
                       │   services.azzurra.chat │
                       │   C/N link via hub-v4   │
                       └──────────┬──────────────┘
                                  │ svc-net (IPv4)
                       ┌──────────▼──────────────┐
                       │   hub                   │
                       │   hub.azzurra.chat      │
                       │   clients 6667/6697     │
                       │   S2S v4:7000 v6:7001   │
                       └──┬───────────────────┬──┘
                          │ leaf4-net (v4)    │ leaf6-net (v6 ULA)
                ┌─────────▼───────┐   ┌───────▼─────────┐
                │  leaf-v4        │   │  leaf-v6        │
                │  leaf4.azzurra  │   │  leaf6.azzurra  │
                │  S2S over v4    │   │  S2S over v6    │
                └─────────────────┘   └─────────────────┘
```

One `bahamut` image is built once and reused for all three ircds; `SERVER_ROLE` env selects the conf template (`hub` / `leaf4` / `leaf6`). Ensures the three binaries are identical — the only prod-relevant combination.

## Host ports

| Service | Plain | TLS   |
|---------|-------|-------|
| hub     | 6667  | 6697  |
| leaf-v4 | 6668  | 6698  |
| leaf-v6 | 6669  | 6699  |

Server-to-server ports (7000/v4, 7001/v6) stay on internal docker networks — not exposed to the host.

## Quickstart

```
cp .env.example .env
# edit .env as needed (defaults are testnet-safe)
docker compose up --build
```

On first run, `cert-init` generates throwaway self-signed certs under `certs/` (one per server), then hub/leaves/services start in order. `scripts/smoke.sh` runs a smoke-test against the stack.

To tear down and wipe services DB:

```
docker compose down -v
```

Plain `docker compose down` preserves the named volume `services-data`.

## Layout

```
.
├── compose.yaml          # top-level topology (build from source)
├── compose.ghcr.yaml     # override — pull images from GHCR instead
├── .env.example          # knobs — copy to .env
├── bahamut/              # one image, three roles
│   ├── Dockerfile
│   ├── options.h_hub     # build-time knobs
│   ├── entrypoint.sh     # envsubst → ircd -F
│   ├── conf.hub.tmpl
│   ├── conf.leaf4.tmpl
│   └── conf.leaf6.tmpl
├── services/
│   ├── Dockerfile
│   ├── entrypoint.sh
│   └── conf.tmpl
├── certs/
│   └── gen-cert.sh       # throwaway self-signed, per server CN
├── scripts/
│   └── smoke.sh          # /map, cross-leaf whois, services round-trip
└── .github/workflows/
    ├── testnet.yml       # CI: up --wait → smoke → down -v
    └── images.yml        # CI: build + push bahamut/services → GHCR
```

## Testing upstream PRs (ephemeral)

Images live at `ghcr.io/azzurra/bahamut` and `ghcr.io/azzurra/services`.

- `:main` and `:sha-<7>` — rebuilt on every infra `main` push.
- `:pr-<N>-sha-<7>` — rebuilt on every infra PR.
- Ad-hoc tags — trigger the `images` workflow manually with a git ref from the upstream bahamut / services repo:

```
gh workflow run images.yml \
  -f ref_bahamut=refs/pull/2010/head \
  -f ref_services=master \
  -f tag_suffix=bahamut-pr-2010
```

Then spin up a testnet pulling the pushed image (no local build):

```
BAHAMUT_TAG=bahamut-pr-2010 \
  docker compose -f compose.yaml -f compose.ghcr.yaml up
```

`compose.ghcr.yaml` is a drop-in override that swaps `build:` for `image: ghcr.io/azzurra/...`. `BAHAMUT_TAG` / `SERVICES_TAG` default to `main`.

`bahamut/Dockerfile` and `services/Dockerfile` support branches, tags, full SHAs, and `refs/pull/N/head` via `BAHAMUT_REF` / `SERVICES_REF` when building locally.

## Synthetic corpora (`harness/`)

Migration-test data generator for [atheme-it#5](https://github.com/0xf01d/atheme-it/issues/5):
drives ~1000 synthetic users + ~1000 channels through REAL services commands
against this testnet, journals every issued command, and snapshots the
resulting flatfiles. Fully automated end-to-end.

```
docker compose -f compose.yaml -f harness/compose.harness.yaml up -d --wait
docker compose -f compose.yaml -f harness/compose.harness.yaml run --rm \
    corpus --seed 1 --users 1000 --channels 1000 --out /out
docker compose -f compose.yaml -f harness/compose.harness.yaml stop services
docker cp azzurra-services:/opt/azzurra/services/data/. harness/out/seed-1/db/
```

or one command for the whole cycle (workload + graceful flatfile save +
`.tar.zst` archive):

```
harness/run.sh SEED [USERS] [CHANNELS]     # default 1000/1000
```

- The overlay sets `SVC_AKILL_CLONES=0` on services — every client connects
  from the runner's single container IP, and the CLONEKILL default (5) would
  autokill the run on the 5th connection (same false positive as grappa's;
  see `services/conf.tmpl`).
- Clients are spread across hub + leaf4 + leaf6: bahamut class `Y:1` caps
  512 local clients per server.
- Two waves per run: register-all first (CS ACCESS ADD needs registered
  targets), then channel work + ambient traffic.
- Per-seed output: `journal.ndjson` (every raw line), `passwords.json`
  (nick/channel credentials — the migration fixtures' known verifiers),
  `manifest.json` (counts/timing/errors), `db/` (nick.db, chan.db, ...).
- `.github/workflows/corpus.yml` runs 10 seeded full-scale runs in a matrix
  and uploads one artifact per seed (smoke-scale 50/50 gate first).

## Non-goals

- Production TLS / real certs — throwaway self-signed only.
- Persistence across `down -v` — ephemeral by design.
- HAProxy / webirc / 6to4 / ident / bopm / stats / real-Azzurra links.
- More than two leaves or a second hub (not now — exercise v4 + v6 S2S paths and move on).

## Related

The greenfield 2026 reboot (REST+SSE server plus IRCv3 listener) lives at [`grappa-irc`](https://github.com/vjt/grappa-irc). This repo is plumbing for the existing legacy stack.
