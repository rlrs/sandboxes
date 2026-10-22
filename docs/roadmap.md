# Roadmap

Open work only, as of 0.9.75 (2026-10-18). How things work today is in
[architecture.md](architecture.md) and [images.md](images.md); why, in
[decisions.md](decisions.md). Calls that need a decision rather than work are
under [Open decisions](#open-decisions).

## Node daemon (`ucloud-noded`)

- **File writes and archive uploads in noded.** Still served by the Python agent; the
  largest setup cost left at 512 rollouts (`node_agent.py`, `runtime/noded/src/exec_front.rs`).
- **Park, wake, migration, commit, reconcile and heartbeats in noded**, then delete the
  Python agent (`direct_service.py`, `direct_warden.py`, `hibernation.py`, `node_agent.py`,
  `heartbeat_sender.py`).
- **Create-path tails.** `runsc_create`, storage prepare (`mkfs.xfs` per create) and
  registry commit set node-side create p95 (`runtime/noded/src/`: `create.rs`, `storage.rs`, `registry/`).
- **CPU QoS for background work** (`cpu.idle`/`cpu.weight` for prefetch, publication, offload, cache fill).
- **IPv6 off on the agent's relay-egress veths** (`direct_network.py`).

## Admission

- **Last two gates for Rust admission:** a mid-burst drain must not report ready early, and
  growth admission at ~180 sandboxes/node must stay under 1 s (`scripts/bench/bench_rl_scale.py`,
  `scripts/bench/live_relay_load_benchmark.py`, flag `sandbox.direct_node_rust_admission`).
- **Delete the Python admission ledger** one release after the flag is on
  (`transition_admission.py`, ledger/pool/growth code in `direct_service.py`, `DemandPublisher`,
  `agent-demand.json`). `admission.py` stays for the gateway.
- **Drop gateway wake admission** (`wake_admission.py`, `resource_admission.py`); the node admits.

## Writable state and memory

- **Hibernate cost independent of bytes written** (gVisor patch 0008, external filestore);
  today a park after large writes takes tens of seconds (`runtime/gvisor/`, `direct_warden.py`).
- **One writable backing:** node-local XFS project quotas and reflink instead of per-sandbox
  ublk, `mkfs.xfs` and the growth poller (`storage_native_*.py`, `runtime/storage_native/`).

## RL state primitives

- **Commit, gateway half:** route, `image_commits` table, reconciler and SDK call on top of the
  existing worker export (`node_commit.py`, `commit_steps.py`, builder `publish_commit`).
- **Fork and templates:** not started; open questions are partial forks and cross-deployment commits.
- **Node-side hydration:** warm a worker's cache for a planned group before its creates land
  (`environment_backend.py`, new node route).

## Images and chunk store

- **Python off the image data path:** serve toolkits and unconverted EROFS through nydusd, then
  delete `environment_nbd.py`, `environment_cache.py` and the RAFS reader in `environment_rafs.py`.
- **Docker off sandbox workers** (`DockerOverlay2RootfsStore` in `image_rootfs.py`, `image_eviction.py`).
- **No per-create init copy:** mount `/.ucloud-init` read-only (`runtime/noded/src/guest.rs`).
- **Placement by chunk residency** (`gateway/heartbeats.py`, `placement_choice.py`).
- **S3 outages** still fail commits and fills until S3 recovers (stalls are bounded since 0.9.67).

## Scaling and control plane

- **Cluster create rate:** target ≥ 300 creates/s; ~30/s measured at production's worker cap.
- **Cold start:** ready p50 ~2 min from zero; add image-gated scale-up (`policy.py`, `node_agent.py`).
- **Finish power-of-k:** move wakes to node-gated admission and delete the placement service.
- **Stateless exec ingress:** drop `upsert_exec` and `exec_sessions` once no old worker can join;
  add route tokens for events and files.
- **Split the single gateway host:** first move heartbeats, quarantine, host locks, build
  history and autoscaler state from host-local files into PostgreSQL.
- **Node-loss timers:** run the observe-only experiment; gaps: no alert on `unavailable`, untimed
  suspensions re-quarantined, occupied never-returning workers need an operator.

## Relay

- **Packet-watcher cost at 500 sandboxes/node** is unmeasured (one NFLOG message per packet).

## Open decisions

- **Turn on `sandbox.direct_node_rust_admission`** once the two gates pass.
- **Default the noded flags on** (`direct_node_front_door`, `direct_node_rust_create/exec/pause`,
  `direct_pause_tier`, `direct_local_model_waits`; production already runs them) and delete
  the Python create, exec and pause paths.
- **Retire relay-driven park and wake** once every trainer sets `guest_relay_url`.
- **Registry disk guard on a shared drive:** replace `statvfs` percentages with a byte bound, or turn it off.
- **One memory mode:** pick one of split, RAM or reflink-restore backing and delete the rest.
- **A second store node:** removes the ~1.5 h cache-refill window after a loss, at a monthly cost.
