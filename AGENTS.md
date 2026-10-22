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
- `scripts/`: checks (`check.sh`, `run_tests.py`); `ops/` installers and operator
  tools, `release/`, `images/`, `bench/` (live load benchmarks).
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
PostgreSQL tests, which include every test of routing, the gateway and LocalFleet,
need `UCLOUD_TEST_POSTGRES_DSN` (`check.sh` refuses to run without it); `linux`
tests need root and runsc; `live` tests need a deployment. A local PostgreSQL:

```bash
docker run -d --rm --name sandboxes-test-pg -e POSTGRES_PASSWORD=test -p 127.0.0.1:55432:5432 postgres:17
export UCLOUD_TEST_POSTGRES_DSN=postgresql://postgres:test@127.0.0.1:55432/postgres
```

## Tests

Write tests of four kinds:

- **Decisions.** Logic lives in functions that take state and `now` and return a
  decision (`admission_decision.decide`, `placement_choice`). Test them with plain
  inputs, and with hypothesis for invariants. Time-dependent behaviour is tested
  here, by passing different `now` values, not by waiting.
- **Contracts.** One suite per boundary with a real dependency (PostgreSQL routing
  and relay, registry, S3, the node API), written once and run against each
  implementation. Python and Rust agree through shared recorded traces, not copied
  tests.
- **Scenarios.** Behaviour through public routes on the LocalFleet harness
  (`tests/harness/`), asserting what a client or operator would observe.
- **Real runtime.** A few scenarios with real runsc, ublk and EROFS, run against a
  staging deployment before a release.

Rules:

- Test a behaviour once, at the lowest layer that can show it.
- Wait for end states with `tests.support.wait_until(condition)`. Don't sleep to
  synchronize, and don't assert on elapsed time.
- Don't patch, call or assign private members (`_name`), and don't build objects
  with `object.__new__`. If logic is only reachable that way, move it into a
  function that can be called directly.
- Assert on results (responses, stored state, files), not on calls to mocks, log
  lines, or error text that isn't part of an API.
- Shared fixtures live in `tests/support.py` and `tests/harness/`; don't import
  other test modules.
- A flaky test is fixed or deleted, never retried.
- Don't add tests for code that is about to be deleted, or for `scripts/bench/`.

Much of the existing suite predates these rules. Bring a test in line when you
change it; don't add more of the old kind. `scripts/check_test_rules.py` (run by
`check.sh`) fails when a test module gains violations; after removing some, run it
with `--update` to lower `tests/test_rules_baseline.json`.

## Changes

- Behaviour changes come with tests (see above). Rust ports of Python logic are
  pinned by golden files; regenerate them with the script next to them.
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
