# Working in this repository

Guidance for people and coding agents changing ucloud-sandboxes. The
[README](README.md) says what the service is; [docs/](README.md#documentation)
says how it works.

## Related repositories

- [ucloud-sandboxes-sdk](https://github.com/rlrs/ucloud-sandboxes-sdk): the Python
  client. `scripts/check.sh` expects a checkout at `./ucloud-sandboxes-sdk`
  (gitignored); CI pins the commit it tests against in `.github/workflows/test.yml`.
- [verifiers-ucloud](https://github.com/rlrs/verifiers-ucloud): what clients
  install. Each tag pins an SDK release.

## Layout

| Path | Contents |
| --- | --- |
| `ucloud_sandboxes/` | The Python package: CLI (`cli.py`), gateway (`control_plane.py`, `gateway/`), shared control state on PostgreSQL (`shared_control/`, `routing.py`), node agent and node-side runtime (`node_agent.py`, `direct_*.py`), images and chunk store (`environment_*.py`, `chunk_*.py`, `image_*.py`), model relay (`model_relay.py`), providers (`providers/ucloud`, `providers/hetzner`) |
| `runtime/noded/` | `ucloud-noded`, the Rust node daemon |
| `runtime/managed_process/` | Go: the sandbox init and managed-process supervisor |
| `runtime/chunk_serve/` | Go: the store node's chunk server |
| `runtime/gvisor/`, `runtime/storage_native/`, `runtime/nydusd/` | Pinned upstream builds and their patches |
| `runtime/toolkits/` | Agent toolkits (vf-agents, vf-harness) |
| `scripts/` | Operator, release, image and qualification tools; `scripts/data/` holds their pinned inputs |
| `tests/` | The Python suite (`tests/fixtures/` for recorded inputs) |
| `docs/` | Reference docs, the roadmap and the decision log |

## Setup and checks

Python is pinned in `.python-version` (3.10.13); dependencies are locked in
`uv.lock`.

```bash
uv sync --extra postgres
bash scripts/check.sh
```

`check.sh` runs ruff, `bash -n` and ShellCheck on `scripts/*.sh`, the Python
suite, a wheel build and install check, `go test` for `runtime/managed_process`
and `runtime/chunk_serve`, `cargo test` for `runtime/noded`, and the SDK
checkout's own suite. Set `UCLOUD_CHECK_ALLOW_MISSING_SHELLCHECK=1` or
`UCLOUD_CHECK_ALLOW_MISSING_SDK=1` to skip those parts deliberately.

For the Python suite alone:

```bash
uv run python scripts/run_tests.py                 # every tier but live
uv run python scripts/run_tests.py --tier unit     # fast, no I/O
uv run python scripts/run_tests.py tests.test_routing
```

Each module runs in its own process with a per-test watchdog. A module declares
its tier with a top-level `TEST_TIER = "..."`:

| Tier | Needs |
| --- | --- |
| `unit` (default) | Nothing beyond temporary directories |
| `contract` | Real SQLite or PostgreSQL, localhost HTTP, child processes, the SDK |
| `linux` | Root, runsc, ublk, EROFS or nftables |
| `live` | A deployment; run only by an operator |

PostgreSQL tests skip unless `UCLOUD_TEST_POSTGRES_DSN` points at a scratch
database. Add `--no-fsync` when `eatmydata` is installed.

## Changes

- Behaviour changes come with tests in the same change. Rust ports of Python
  logic are held to the Python behaviour with golden files (for example
  `runtime/noded/src/local_wait/testdata/`); regenerate goldens with the script
  next to them rather than by hand.
- New behaviour that is risky to switch on ships behind a config flag that
  defaults off (`ucloud_sandboxes/config.py`). Once production has run with it
  on and there is no way back, make it the default and delete the old path.
- Update the reference doc that describes what you changed. Open work goes in
  [docs/roadmap.md](docs/roadmap.md); a choice between alternatives, with its
  reason, goes in [docs/decisions.md](docs/decisions.md).

## Releases

Releases are cut from `main`:

1. Bump `version` in `pyproject.toml`.
2. Add a `## X.Y.Z - YYYY-MM-DD (components)` entry at the top of
   [CHANGELOG.md](CHANGELOG.md) saying what changes for operators and clients.
3. Tag the merge commit `vX.Y.Z`.

Node bundles are repacked from the pinned runtime builds with
`scripts/repack_node_bundle.py` and `scripts/derive_builder_bundle.py`; see
[docs/deployment.md](docs/deployment.md).

## This repository is public

Never commit:

- credentials, tokens or private keys (local ones live in the gitignored `.env`
  and `.hetzner/`);
- production identifiers: public or private addresses of production machines,
  the production hostnames, bucket names, UCloud project or job IDs;
- home-directory paths;
- raw run output: logs, telemetry and database exports, per-task results.
  Summarize a measurement in the doc or decision it supports and keep the raw
  data outside the repository.

Scripts that talk to a deployment read `UCLOUD_SANDBOX_URL`,
`UCLOUD_SANDBOX_API_TOKEN` and `UCLOUD_RELAY_URL` instead of hard-coding them.
