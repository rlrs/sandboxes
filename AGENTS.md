# Working in this repository

## Layout

- `sandboxes/`: the Python package. CLI (`cli.py`); gateway
  (`control_plane.py`, `gateway/`); shared state on PostgreSQL (`shared_control/`,
  `routing.py`); node agent (`node_agent.py`, `direct_*.py`); images and chunk store
  (`environment_*.py`, `chunk_*.py`); model relay (`model_relay.py`); providers.
- `runtime/noded/`: `ucloud-noded`, the Rust node daemon.
- `runtime/managed_process/`, `runtime/chunk_serve/`: Go components.
- `runtime/gvisor/`, `runtime/storage_native/`, `runtime/nydusd/`: pinned upstream
  builds and patches. `runtime/toolkits/`: agent toolkits.
- `scripts/`: operator, release and image tools (`scripts/data/` holds pinned inputs).
- `tests/`, `docs/`.

The SDK is a separate repo, [ucloud-sandboxes-sdk](https://github.com/rlrs/ucloud-sandboxes-sdk);
`scripts/check.sh` expects a checkout at `./ucloud-sandboxes-sdk` (gitignored).

Some names still say `ucloud` because they are wire or on-disk contracts with
running deployments and the SDK: `UCLOUD_*` environment variables, `X-UCloud-*`
headers, `/etc/ucloud-sandboxes` and other host paths, `ucloud-sandbox-*` systemd
units, the `ucloud-noded` and other runtime binaries, and the `ucloud-sandboxes.`
label prefix. Renaming them needs a planned migration, not a find-and-replace.
`providers/ucloud` is the UCloud provider itself.

## Checks

```bash
uv sync --extra postgres
bash scripts/check.sh                              # everything
uv run python scripts/run_tests.py                 # Python suite only
uv run python scripts/run_tests.py --tier unit     # fast subset
```

Test modules declare `TEST_TIER` (`unit` by default, `contract`, `linux`, `live`).
PostgreSQL tests need `UCLOUD_TEST_POSTGRES_DSN`; `linux` tests need root and
runsc; `live` tests need a deployment.

## Changes

- Tests come with behaviour changes. Rust ports of Python logic are pinned by golden
  files; regenerate them with the script next to them.
- Risky behaviour ships behind a config flag that defaults off. Once production runs
  it and there's no way back, make it the default and delete the old path.
- Update the doc that describes what you changed; keep docs short. Open work goes in
  `docs/roadmap.md`, a choice between alternatives in `docs/decisions.md`.

## Releases

Cut from `main`: bump `version` in `pyproject.toml`, add a
`## X.Y.Z - YYYY-MM-DD (components)` entry to `CHANGELOG.md`, tag `vX.Y.Z`.

## This repository is public

Never commit credentials, production identifiers (machine addresses, production
hostnames, bucket names, UCloud project or job IDs), home-directory paths, or raw
run output (logs, telemetry, database exports). Summarize measurements in the doc
they support. Scripts read `UCLOUD_SANDBOX_URL`, `UCLOUD_SANDBOX_API_TOKEN` and
`UCLOUD_RELAY_URL` instead of hard-coding them.
