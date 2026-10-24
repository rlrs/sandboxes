# Changelog

## Unreleased

- **Operator scripts**: `scripts/images/import_image_recipes.py` only exports
  bundles; register names with `build_image_index.py register`. The unused
  `preflight_environment.py`, `build_environment_canary.sh` and
  `guest_conformance_probe.py` are gone.
- **ucloud-noded listens with a backlog of 4096, as the Python agent did.**
  `TcpListener::bind` gave it 128, so a burst of more connects than that, while
  the accept loop was busy, had SYNs dropped: those clients waited 1, 3, 7 s for
  retransmits and could pass the gateway's 5 s connect timeout. The held
  job-read test hit this in CI (2995 of 3000 reads held at its deadline).
- **Buffered worker RPCs never reuse a connection the gateway asked to close**
  (RFC 9112 9.6). aiohttp pooled such a connection unless the answer also said
  `Connection: close`. ucloud-noded and the Python agent always say so, so
  production was not affected. A gateway upload test's fake worker closed
  silently, which let the next RPC reach a closing socket and fail with a
  retryable 502 (`worker RPC reconnect replay refused`) although the worker never
  received it. Such requests now get a connection that closes after its one
  response. No request is resent.
- **The autoscaler no longer has a destructive node-loss path.** No provider
  could declare one since UCloud's loss dispositions were removed, so the
  `reconcile`/`autoscaler` output drops its always-empty node-loss fields
  (`destructive_node_loss_job_ids`, `destructive_stop_job_ids`,
  `lost_sandbox_ids`, `fencedNodeLossHeartbeats`, `persistedNodeLossDemand`,
  per-node `permanentlyLost`) and the metrics scale-down event drops
  `destructive_job_ids`.
- **The gateway no longer serves workers older than 0.9.66.** Heartbeats must
  carry every runtime metric field (including `memory_backing`,
  `resource_evidence` with its host CPU counters and device byte totals, and
  `cpu_check_ms`); persisted heartbeat rows must too. The gateway stops checking
  the archive-upload, relay-wake-fence, resource-phase and body-keepalive
  capabilities, a builder without an admission-capacity label is closed instead
  of getting four slots, and archive extraction no longer recognises a static
  helper that predates `files extract`. Before restarting the gateway, run
  `scripts/release/verify_heartbeat_upgrade.py` with this package against
  `control-state.sqlite`; it must validate every row. Rolling back to 0.10.0
  needs nothing: it accepts everything this release writes.
- `ucloud.platform.postgres.duration` no longer records a `lock_query` phase. No
  query was timed into it, so it was always 0.

## 0.10.0 - 2026-10-22 (gateway; workers; store node)

- **The project is now `sandboxes`.** The distribution, the Python package
  (`import sandboxes`) and the CLI (`sandboxes …`, `sandboxes-storage`,
  `sandboxes-snapshot-gc`) drop the `ucloud` prefix,
  and the provider entry-point group is `sandboxes.compute_providers`. Gateway
  units and node launchers call the new binaries; a gateway upgrade uninstalls the
  old `ucloud-sandboxes` distribution from the venv, and repacking a node bundle
  removes the old package. Environment variables, headers, host paths, unit names,
  runtime binaries and labels are unchanged.
- **The SQLite relay importer is gone.** `python -m sandboxes.shared_control
  import-idle-relay`, the relay's startup check of `model-relay.sqlite3` and its
  check of the import marker are deleted; the relay has run only on PostgreSQL since
  0.5.114. A deployment still on an older relay, or with an unfinished import,
  imports with 0.9.75 first. The schema keeps the unused `relay_imports` table so a
  rollback still opens it.
- **Routing runs only on PostgreSQL.** The SQLite routing backend is gone, with
  `python -m sandboxes.shared_control.routing_cutover`, the gateway's routing write
  process and the placement file lock. `<data_root>/routes.sqlite` must hold the
  PostgreSQL routing descriptor, as production's does: the gateway, autoscaler and
  GC now fail at startup on a missing file or a SQLite database instead of creating
  one. A fresh deployment creates the descriptor with `python -m
  sandboxes.shared_control migrate ... --routing-file <data_root>/routes.sqlite
  --routing-schema <schema>`. A deployment still on SQLite routing cuts over with
  `routing_cutover` from 0.9.75 first. Nothing else changes for a deployment already
  on PostgreSQL routing, and rolling back reads the same descriptor and schema.
  `orjson` is no longer a dependency: only the SQLite fleet scan used it.
- **`ucloud-sandboxes-registry-migrate` is deleted.** The filesystem-to-S3 registry
  migration was never used; the registry stays on the filesystem.
- **Removed unused code.** The in-guest exec agent (`ucloud-sandbox-init agent`,
  `sandboxes/guest_agent.py`), which was never wired into the exec path, and the
  flat-image qualification modules (`flat_image_qualification.py`,
  `oci_flat_delta.py`), which only their own test imported.
- **A rejected create no longer overbooks the next candidate.** When a power-of-k
  create's first worker refused it (admission closed or deferred), the gateway sent
  it to the next sampled worker without checking fit again, although this process's
  concurrent creates may have filled that worker since the choice. The gateway now
  checks fit, with its current in-flight charges, before each later candidate and
  skips one that is full; when none fits, the create queues with a retryable 503.
- **Power-of-k is the only create placement.** Ranked placement, still the code
  default, is deleted with what only it used: the fleet scan and reservation in
  `gateway/placement.py`, the 503 `gateway_placement_busy` and 501
  `sandbox_group_create_unavailable` answers, and the registry layer-metadata cache
  with the manifest fetches each heartbeat started for it. `gateway_create_placement`
  is no longer written; a config naming `power_of_k` still loads and any other value
  is refused. The placement service claims wakes only, as it already did under
  power-of-k. A rollback to 0.9.75 must restore the previous `deployment.json`:
  without the key, 0.9.75 places with ranked.

## 0.9.75 - 2026-10-12 (gateway)

### Gateway

- **A retryable image-check failure no longer fails a create.** Before dispatching a create, the gateway makes sure the chosen worker has the image. When that check failed with a retryable error (the gateway's own `node_request_timeout`, `node_transport_error`, `node_dns_unavailable`, `http_request_capacity_exhausted`, or any 5xx a worker marks `retryable`), a group create marked every member `failed` and answered a non-retryable 502, and a single create answered 502. On 2026-10-12 this failed a group of 8 during a scale-up. Since no create had been dispatched, such a failure now rejects that worker: the members re-plan on another worker or stay pending with a retryable 503. A non-retryable image error (a missing image, a registry error the worker reports without `retryable`) still fails as before. `_image_check_rejection_reason` in `gateway/create.py` serves the group, single and legacy create paths.

## 0.9.74 - 2026-10-12 (gateway; workers)

0.9.73 plus one fix: the admission service's claim tokens read randomness with
the raw `getrandom` syscall (`guest::random_bytes`). 0.9.73 called
`libc::getrandom`, which a static musl link resolves to address 0 (the 0.9.44
canary crash); `build_pinned.sh`'s link check refused the build, so 0.9.73 was
tagged but never built or deployed.

## 0.9.73 - 2026-10-12 (gateway; workers)

Node admission moves into ucloud-noded (docs/admission-port-3c.md, steps 3c.0–3c.4), behind `sandbox.direct_node_rust_admission`, **off by default**: with it off, admission behaves as in 0.9.72. The flag is turned on only after the 3c.5 gates (memory pressure, density, 1,024-rollout readiness and drain) pass on workers.

### Node

- **Node admission's decision is a pure function** (docs/admission-port-3c.md, step 3c.1). This changes no behavior.
  - `ucloud_sandboxes/admission_decision.py` holds `gate` and `decide(metrics, ledger, request) -> Grant | Wait | Refuse`, the same split as noded's `admission/decide.rs`.
  - `DirectSandboxService._active_admission_guard` now drives them. It keeps the locking, sampling, owner validation, requotes and waits.
  - The Rust port's goldens are recorded from `decide` and are byte-identical to before. The generator also checks every case against the guard.
- **CPU is gone from node admission (decision G).** Every caller already passed `check_cpu=False`, so this removes dead code:
  - the guard's `check_cpu` parameter and its CPU-retry loop;
  - `_CPU_ADMISSION_RETRY_SECONDS` and `_CPU_ADMISSION_DEADLINE_SECONDS`.
  - The 0.21 s memory-wait interval is unchanged and is now `admission_decision.RETRY_SECONDS`.
  - The gateway's CPU checks in `resource_admission.py` are unchanged.

### noded

- **Admission port, steps 3c.3 and 3c.4: one node ledger with the flag on** (docs/admission-port-3c.md). With `sandbox.direct_node_rust_admission` off nothing changes; the flag stays off until the 3c.5 gates.
  - The agent admits through `service.admission` (`ucloud_sandboxes/admission_backend.py`): `LocalAdmission` is its own ledger, `NodedAdmission` calls noded's admission routes. With the flag every call site goes to noded: creates, restores (Python still quotes the cost and revalidates after the grant), captures, publications, delete's cancel, drain close/open (synchronous, before the drain row), relay continuations, the agent's own managed starts, and every growth writer. Refusals raise the same exceptions with noded's messages, so client answers are unchanged; an unanswered or unsendable request is a safe retry. The agent's growth state, `DemandPublisher` and growth executor are off.
  - Hellos reassert every claim the agent holds, so a restarted noded takes them back; after a hello the agent aligns noded's gate with the drain row and sends growth events its start could not.
  - The heartbeat reads noded's snapshot first, then the agent's exec leases and the inventory, and skips its sample when noded is down, recovering, or restarted in between. The transient revision is the agent's plus noded's, in lifecycle answers too.
  - noded's creates take the startup slot and capacity in process and pass the claim to the agent's `creates/admit` (`"admitted": {"token"}`), which then holds only the lifecycle lock and layout; noded releases the claim after finish returns. Refusals are the agent's bodies byte for byte.
  - noded gains `POST /internal/v1/growth/launch` and the `bind` growth event (the agent's own managed start), per-pool `slots` in the snapshot, fresh registry reads for owner validation, and `--observations-file` for `--admission-only`.
  - The admission suites run every test against a real `ucloud-noded --admission-only` too (built with cargo), and `LocalFleet(rust_admission=True)` runs the drain scenarios against one.
- **Admission port, step 3c.2: ucloud-noded's own admission service, dead behind the flag** (docs/admission-port-3c.md). With `sandbox.direct_node_rust_admission` off nothing changes; with it on, the agent does not call the service yet (3c.3), so the flag stays off in production.
  - `runtime/noded/src/admission/service.rs` drives the 3c.1 core as `DirectSandboxService` does: one ledger, the startup, restore and publication pools, the managed growth intents and forecasts (durable writes through the registry under per-key turns, detached from callers that go away), credit expiry, forecasts credited from noded's own resident samples after every sampler pass, the drain gate, and the transient revision (CLOCK_MONOTONIC ns, +1 per create or restore claim and release). Admissions wait on change notifications, requote growth each pass and revalidate owners against the registry.
  - The internal socket serves the §3.3 routes once the node runs its own admission: `startup`, `restore`, `observe`, `publication`, `{token}/release`, `cancel`, `close`/`open`, `snapshot`, `growth/events` and `growth/continuation` (no `growth/launch`, decision J). Tokens are 128-bit, bound to the agent session, owner and kind; release is idempotent (404 when unknown). A client that disconnects while queued gives its place back, and a grant whose answer is never sent is released.
  - Sessions and recovery (§3.5, decision F): `hello` may carry `reassert` claims; a hello under a new session drops every claim and waiter of other sessions. A new service grants nothing (`503 admission_recovering`, snapshot included) until the agent's hello, or until 2 s pass with the agent's socket unreachable. Requests from a session without a hello get `503 agent_hello_required`.
  - With the flag on, the pause policy's reclaim demand comes from this ledger (`LedgerDemandSource`) instead of `agent-demand.json`, local waits' growth events apply to it in process (same 50 ms batches, per-item errors), and noded's job start and terminal observation go through an `AdmissionPort`: the agent's `/internal/v1/managed/*` (unchanged, and still the only path with the flag off) or the in-process `LocalAdmission`. noded's creates still use the agent's admission (3c.4).
  - `ucloud-noded --admission-only --internal-unix P --registry R --node-control-token-file T [--admission-config C] [--metrics-file M]` serves only the admission routes, for the agent's tests.
  - `Registry::growth_intent_steps` is `growth_intent_batch` with job identities; the memory store gains `observe_allocation`, re-read before each growth quote.
- **Admission port, step 3c.0 (b, c, d): plumbing only (docs/admission-port-3c.md).** Admission behaviour does not change; with the new flag off nothing changes at all.
  - `sandbox.direct_node_rust_admission` (default off) requires the front door, Rust creates, Rust execs and the Rust pause tier. It renders agent `--rust-admission` and noded `--rust-admission --internal-unix <state>/direct-runtime/noded/noded.sock`.
  - With the flag, ucloud-noded serves the agent on that Unix socket: 0600 in a 0700 directory, connections from any uid but its own closed unread (`SO_PEERCRED`), and the node control bearer token on every request. Requests carry `X-UCloud-Agent-Session` and answers carry `X-UCloud-Noded-Session`. Only the handshake is served: `POST /internal/v1/admission/hello` answers `{noded_session, admission_open, config_sha256}` (the drain row as noded reads it, and the loaded configuration's digest; null until the node is loaded), and `GET …/ping` answers too. Any other `/internal/v1/admission/` path is a 404 `admission_route_unknown`.
  - The agent's `NodedClient` (`ucloud_sandboxes/noded_client.py`) keeps pooled connections. It discards a connection noded closed while it was idle, and raises `NodedUnavailable`, `NodedLost`, `NodedRefused` or `NodedProtocolError`. With the flag, the agent says hello at start, retrying in the background while noded is down, and again whenever it sees a new noded session.
  - noded's pressure sample also reads MemTotal and memory PSI "full" avg10 (unknown when unreadable, as in `runtime_metrics`). No current consumer reads either yet.
  - `GET /internal/v1/creates/config` gains an `admission` block read from the live service: slots, the wait, the active capacity, the physical floor, growth observation age, prefetch bound, publication lanes and retry interval. It is under the digest, so every node's `config_sha256` changes once. noded parses the block into `AdmissionConfig` and keeps it, with the digest, in its `CreateConfig`.
- **Phase 3b: ucloud-noded samples managed sandboxes' resident memory** (docs/admission-port-3c.md, step 3c.0(a)), behind `sandbox.direct_node_rust_admission` (above). With the flag off nothing changes; noded samples managed sandboxes only when its `--rust-admission` and the agent's `admission.rust_admission_enabled` both say so.
  - The pause tier's 1 s sampler pass now also measures every owned, parkable, managed sandbox, running or paused, and after each pass replaces `<state_root>/noded/resident.json` (0600, atomic rename): `{seq, session, samples: [{sandbox_id, generation, <every ResidentMemorySample field>}]}`, `sampled_at` in CLOCK_MONOTONIC seconds. The policy's clock is now CLOCK_MONOTONIC itself rather than an offset from the daemon's start, so the agent can compare it with `time.monotonic()`.
  - The agent stops its own sampling pass. Every resident reader (growth forecasts, resident waits, relay parks, pause decisions, the heartbeat's memory observation) reads noded's observations through `PublishedResidentMemory`, and the forecast refresh that followed each of the agent's passes follows each new `(session, seq)`, polled every 250 ms. Missing, invalid or foreign-writable files and samples are no observation; samples older than the 30 s growth age charge whole bounds, so a stopped noded's credit expires by itself. A park, restore or delete hides older observations until a newer pass. The agent's own reclaims still measure on demand, and the newer observation wins.
  - noded's sampler now gives `peak_bytes` exactly as Python does for a negative `memory.peak` (the current charge, not 0). `runtime/noded/tests/resident_python_parity.rs` samples 26 fake cgroup trees with both samplers and requires equal fields, and requires the agent's reading of `resident.json` to equal noded's samples bit for bit.

## 0.9.72 - 2026-10-11 (gateway; workers)

### Node

- **Managed-process state is host-visible (docs/managed-process-reads.md).** A new managed-process sandbox binds `/.ucloud-managed` from its bundle's `rootfs/.ucloud-managed` (`rbind,rw,nosuid,nodev,noexec`, the last OCI mount), so the supervisor's `state.json` and logs land in the sandbox's host overlay upper, which park seals, moves ship, delete removes and commit drops. The node creates the directory (root, 0700, never through a symlink) before `runsc create` and `restore`, and refuses a config whose mount names another bundle.
  - A move regenerates the config with the mount at the new bundle, but only when the shipped upper has `.ucloud-managed`; a sandbox created before this release has none, so it stays legacy (no mount), as its checkpoint's mount table requires.
  - The park/wake ledger fingerprint is total over what the guest left, since the guest owns that directory now: no `state.json` gives the "no job" constant, a regular file of at most 1 MiB the SHA-256 of its bytes (valid or not), and anything else (a symlink, FIFO, directory, oversize file) a constant per reason, never followed or opened. A guest can no longer make its own park or wake fail, and the stopped guest's same files give the same value at both. Before, the host never saw the guest's ledger, so the fingerprint was the constant "no job"; it is now the ledger at park. Sandboxes parked before this release keep that constant and still wake.
  - The supervisor (`runtime/managed_process`) persists `state.json` the first time a stream truncates (`stdout_truncated`/`stderr_truncated`, `sequence` + 1), so a host reader learns of it before the exit.
- **A parked sandbox's job record is exact at park.** After the checkpoint the agent derives the job record from the stopped guest's bound state (file sizes while running, persisted counts once terminal, plus `cursor`) and keeps it beside the bundle (`.ucloud-managed-process.json`; an import derives it again from the shipped upper). While parked, the node's sandbox record carries it as `managed_process`: `GET /v1/sandboxes`, park responses, wake failures that leave the sandbox parked, and the heartbeat inventory. A job that wrote or exited after the last read and then parked is no longer invisible to the gateway's cache.
- **The agent keeps the growth ledger for launches noded dispatches.** `POST /internal/v1/managed/launch`, `/bind` and `/terminal` on the agent's socket commit and admit a launch before noded's dispatch, bind an imported primary and retire a terminal one, through the same helpers as `start_managed_process` (docs/managed-growth-admission.md).
- The local fleet's fake `runsc` honours the `/.ucloud-managed` bind mount, and restore requires the checkpoint's bind destinations.

### Gateway

- **The chunk-index backup reaches the store node by name again.** Since 0.9.70 the store node listens on `0.0.0.0`, and `backup_gateway_state.py` took its ssh host from that address; it now uses the host in the store node's URL (`sandboxes-store-1`).
- **Job reads can wait at the gateway without holding a thread** (docs/managed-process-reads.md). On 2026-10-11 verifiers-ucloud's job polling, about 3 reads/s per sandbox, kept the node agents busy; reads that wait for a change replace it.
  - `wait_ms`, `after`, `offset` and `limit` reach the worker unchanged. The gateway allows a read with `wait_ms` the wait (at most 25 s) plus the usual 60 s.
  - Waiting reads go through the asynchronous response owner, as exec event polls do, on their own connection lane (1,024 per worker), so about 1,000 held reads hold no handler thread. Qualified with two handler threads, 64 held reads and a health request served meanwhile.
- **Log reads keep the gateway's job cache fresh.** The record a worker adds to a log response is cached like a status record, fenced on generation and sequence. Within a sequence the cached byte counts no longer shrink when an older read finishes last.
- **A parked sandbox's job record is exact.** A worker's record of a parked managed sandbox may carry `managed_process`, the job record read after the checkpoint. The gateway caches it, validated and fenced like a read's, from park responses, wake failures, worker listings, wake-time publication and heartbeat inventory (`SandboxInventoryEntry.managed_process`), so a job that wrote or exited after the last read and then parked is not mistaken for idle; a parked logs read below the new count wakes the sandbox.
- **A parked sandbox answers waiting job reads from the cache.** A status read with `wait_ms` returns the cached record with `Retry-After: 1`. A logs read with `wait_ms` at or past the cached byte count for its stream returns no data, the cached record and `Retry-After: 1` without waking the sandbox; a read below it still wakes it. The same holds while the worker reports a lifecycle transition. Records served from the cache carry the worker's `cursor`.

### noded

- **ucloud-noded serves managed-process jobs without the Python agent (docs/managed-process-reads.md).** On 2026-10-11 about 200 OpenCode/Pi sandboxes per worker kept the agent's GIL about 90% busy in job status reads, each a `runsc exec … ctl` that took the request lock and the warden flock and thawed a paused sandbox; 165 starts, execs and deletes timed out at the gateway in four minutes.
  - Managed-process sandboxes bind-mount `/.ucloud-managed` from `<bundle>/rootfs/.ucloud-managed` (created root-owned, 0700, before `runsc create`), so the supervisor's `state.json` and logs land in the sandbox's own overlay, where the host can read them.
  - `GET /v1/sandboxes/{id}/jobs/{job}` and `…/logs/{stdout|stderr}` are answered by ucloud-noded from those files for sandboxes it owns that are running, paused included: no guest entry, no thaw, no locks, no activity mark. Records are Python's plus `cursor` (`<sequence>.<stdout_bytes>.<stderr_bytes>`); log chunks carry the `job` record. Files are opened without following symlinks and must be regular; a symlink, FIFO, oversize or malformed `state.json` is a 409 `managed_process_state_invalid`.
  - `wait_ms` (up to 25 s) holds a read until the cursor differs from `after`, the stream has bytes past `offset`, the job is terminal, or the wait ends. One inotify instance, a watch per state directory only while reads wait on it; a directory that goes away mid-wait (park, delete) returns the last record read. Up to 16,384 held reads (then a retryable 503 `node_job_reads_busy`); the front door now takes 16,384 connections.
  - `POST /v1/sandboxes/{id}/jobs` and `…/jobs/{job}/signal` run in ucloud-noded too (with `--rust-exec`): Python's validation and messages, the same control payload and launch digest, the launch and bind through the agent's `/internal/v1/managed/{launch,bind}`, the `ctl` under the exec fences with thaw-on-start, retried only on `control_not_connected` within 15 s. Terminal records reach the agent's growth ledger once per job through `/internal/v1/managed/terminal`.
  - Sandboxes created before this release have no mount and keep their job routes in the agent, as do parked ones and anything the daemon does not read exactly as Python does.

## 0.9.71 - 2026-10-11 (gateway)

- **A build that failed in our machinery retries in seconds, not 10 minutes.** A sandbox build that fails with `sandbox build infrastructure:` (the chunk store, S3, the registry: not the recipe) is retried after 30 s, doubling up to 10 minutes, at most 6 times in all; recipe failures keep 3 attempts 10 minutes apart. On 2026-10-11 a TMax image's first build failed so, and its rollouts timed out (`nginx 504`, stream timeouts) during the 10-minute backoff before the second attempt built it in 95 s.
- **The image index reports finished builds as ready.** A recipe's row became ready only when a create or `ensure` next asked for it, so the index (summary, names, `task-ids`) showed 70 built images as `building`. The index's reads first settle rows whose builds finished (as `ensure` does, submitting nothing), at most every 10 s per process.

## 0.9.70 - 2026-10-11 (gateway; workers; store node)

- **Production names its machines, not their addresses.** UCloud restarted the gateway and store-node VMs at 04:24 UTC on 2026-10-11, and both came back with new private addresses. Every address in the configuration was a literal, so new workers could not heartbeat, the store node's services could not bind, and images stored under the old registry address could not be imported. The configuration now names the VMs by their UCloud DNS names (`sandbox-gateway-gateway-1`, `sandboxes-store-1`), which every VM in the project resolves, and services bind `0.0.0.0` (docs/operations-runbook.md).
- **Sandboxes reach private services by name, whatever their egress.** A relay named by DNS has a stable virtual address (198.18.0.0/15, from its name); relay-only sandboxes already reached it through their `/etc/hosts`. Direct-egress sandboxes now do too: with relays configured their `/etc/hosts` is replaced (as Docker replaces it) to name loopback and every relay at its virtual address, in the agent's and ucloud-noded's create paths, and the worker's relay table translates those addresses to the relays' current ones (`bridge_nat`), re-resolved every 2 s, marking exactly those flows past the private-destination denies (`bridge_forward`). Production configures the gateway's relay (`default`) and the store node's package cache (`packages`) this way, so a client's `guest_relay_url` and sandbox builds' proxy are names that survive address changes (docs/network-policy.md). `direct_network_allow_tcp` keeps literal addresses only: ucloud-noded leaves creates with DNS-named entries there to the agent.
- **Node-local model waits follow a relay named by DNS.** A relay whose name resolves only to private addresses takes the plaintext path, matched at those addresses after translation (resolved when the node agent starts). Before, only IPv4-literal relays did, and naming the relay would have silently turned pausing through model calls off.
- `scripts/install_ucloud_observability_stack.sh` takes `--private-bind-ip 0.0.0.0` with `--endpoint-host <name>`.

## 0.9.69 - 2026-10-10 (gateway; store node's package cache)

- **Builds fetch GitHub repositories with a token, through the package cache.** GitHub limits anonymous git downloads from UCloud's address ("temporarily limiting some unauthenticated downloads ... authenticate", a 401 partway through a clone, on and off within minutes; 2026-10-10), and every OpenSWE recipe clones from GitHub.
  - With `chunk_store.store_node.package_cache.github_token_file` set, the package cache serves git's read-only smart HTTP for GitHub under `/github/<owner>/<repo>/` (`info/refs?service=git-upload-pack` and `git-upload-pack` only; no pushes, no other paths), forwarding to `https://github.com` with the token. Renamed repositories' redirects stay on the proxy. The token is read from the file (reread when it changes), never logged and never given to a build.
  - Sandbox build steps get git's `url.<cache>/github/.insteadOf https://github.com/` (and `http://`) through `GIT_CONFIG_COUNT` (git 2.31 and later; older git fetches anonymously as before), unless a recipe sets its own `GIT_CONFIG_COUNT`. A clone keeps its `https://github.com/` remote; nothing changes in recipes or images.
  - Checked with a real `git clone` (2.34) through the proxy against `git http-backend`.

## 0.9.68 - 2026-10-10 (gateway; store node's chunk index; builders)

- **Chunk-store calls outlast Hetzner's outage windows.** Besides single stalls, Hetzner S3 has windows where every request fails for 45-130 s (2026-10-10, even with 1 worker and 8 builders): 0.9.67's attempt-bounded retries ran out inside them and failed the build (31 of the first 90 SWE-smith imports). Every object-store read, HEAD and put (idempotent: content-addressed) now retries transient failures (transport errors, 5xx, 405, 408, 429, and boto3's spent retries) for up to 5 minutes (`STORE_RETRY_SECONDS`), backing off at most 15 s. Builders wait up to 10 minutes for the index's commit, as for registration, so the index's own retries finish first.
- **A sandbox build whose layer has no file data no longer fails.** nydus-image v2.4.5's merge writes its 1 MiB default chunk size when no merged layer has a blob, a bootstrap nydus's own `check` refuses ("unsupported RAFS v6 bootstrap" here). The stacked bootstrap gets its parent's chunk size back, which every chunk in it has. Five TBLite eval images (directories and modes only) failed this way.

## 0.9.67 - 2026-10-10 (gateway; store node's chunk index; builders)

- **Publishing to the chunk store no longer waits out stalled S3 requests.** About 1 Hetzner S3 request in 100 stalls for 20 s or more, a few for minutes (2026-10-10 probe of 200 metadata reads: p50 0.15 s, p99 124 s). Sandbox builds spent 6 s of a median 86 s running their steps and the rest publishing or waiting for one of the 3 publishing slots held by a stalled build.
  - Presigned object-store reads abandon a response that has sent nothing for 5 s (`STALL_SECONDS`) and retry at once, up to 6 attempts; the whole-request deadline is unchanged. HEADs use their own client with 5 s connect and 10 s read timeouts; uploads keep 120 s.
  - Hetzner's occasional 405 on a valid presigned GET, and 408 and 429, are retried like 5xx.
- **Metadata named by its digest is read once per process.** Bootstraps and chunk maps never change, so each process keeps the recently read and written ones (256 MiB): a sandbox build reads its parent's bootstrap once, not once per task. Existence checks (`size`) always ask the object store.
- **Registration reads a base's blobs once.** The chunk index keeps each blob's tail table and whether its layout is written, by blob id: a task image on a 12-layer base registered with about 50 S3 requests, now about 8.
- Object-store calls slower than 2 s are logged (`slow object-store get|stat|put`).

## 0.9.66 - 2026-10-08 (gateway)

- **The image index (docs/image-index.md).** The gateway's index of training image names: every name verifiers asks for, backed by an image or a base already in the chunk store.
  - A name is `prepared` (the whole image is in the chunk store, ready from registration) or a recipe (built on first use on its prepared base). The gateway refuses either when nothing it needs is prepared.
  - Each name carries its environment, the dataset tasks that use it and its source.
  - `GET /v1/image-index` (names and tasks per environment, by state), `/names`, `/name?name=` and `/task-ids?environment=` (the tasks a trainer may sample: every name not failed for good), readable with the SDK key; `ucloud-sandboxes image-index summary | show | list | task-ids | export-task-ids` reads them.
  - `POST /v1/image-recipes` takes `prepared_reference`, `environment`, `tasks`, `source` and `"partial": true` (register the rest and list the refused).
- `scripts/build_image_index.py` exports and registers all nine training environments from the 2026-10-01 inventory: 66,786 names, 130,253 tasks.

## 0.9.65 - 2026-10-08 (gateway)

- **The sandbox builds service no longer stops on a build finishing while it lists jobs.** `Spool.pending` sorted job files by their mtime, so a job file removed between the listing and the stat raised `FileNotFoundError` out of the service's loop.

## 0.9.64 - 2026-10-08 (gateway)

- **Build sandboxes get a `/tmp` of half their memory** (4 GB by default; other sandboxes keep 64 MB). Docker's `/tmp` is disk, and recipes that download into `/tmp` by name failed in a sandbox: openswe-218 fetches MongoDB's archive there.
- **A heartbeat pull records its outcome before waking the requests waiting on it**, so they never read the metrics without it.

## 0.9.63 - 2026-10-08 (gateway; builders)

- **Packing many small chunks is no longer quadratic.** A pack writer summed its entries on every chunk it took, so a layer of small files spent most of its stacking time there (a 495 MB OpenSWE layer of 15,546 chunks packed at 6 MB/s); locally, 6,000 small chunks pack in 1.5 s instead of 5.0 s. The layer step now reports time waiting on uploads (`pack_upload_wait_s`) apart from the uploads themselves (`pack_put_s`).

## 0.9.62 - 2026-10-08 (gateway; builders)

- **Four pack uploads in flight (was two).** Uploads to the chunk store's bucket run about 11 MB/s per stream from UCloud and 66 MB/s with four (70 with eight); 0.9.61's sandbox builds with hundreds of MB of new chunks spent most of their stacking time uploading at 5-6 MB/s.

## 0.9.61 - 2026-10-08 (gateway; builders)

- **Faster stacking for sandbox builds.** The commit filter wrote a layer's names in string order (`a.b` before `a/b`), the converter needs path order, so every sandbox layer was copied twice before `nydus-image create` (gigabytes for OpenSWE). The filter writes path order now (future commit diff IDs differ from older releases'). Packs upload to S3 and commit while the next one fills (two in flight), for builders' conversions too. `sandbox_builds.stack_slots` (default 3, was a fixed 2) sets how many builds filter and stack at once; the layer step records its reorder, create and pack times.

## 0.9.60 - 2026-10-08 (gateway)

- **Rebuilding a pinned recipe image no longer breaks ensure.** A rebuild names a new digest under the image's tag, and the pinned owner's durable reference refused it ("digest is immutable"); ensure answered a retryable 503 for every name in the call, so the SDK's `wait_for_images` never finished. The reference (and its environment's) now moves to the new digest, and a pin that fails is retried on the next call instead of failing the others. Found rebuilding the 11 images 0.9.59 fixed.

## 0.9.59 - 2026-10-08 (gateway; store node; builders)

- **Images built into the chunk store stay readable by nydusd.** nydusd's blobs are rebuilt on the store node from stored chunks byte for byte, but the index keeps the first stored copy of a chunk, and conversions without nydusd blobs (M1/M2 era) stored a chunk raw when zstd saved under 3% where nydus compressed it. A new layer sharing such a chunk got a blob the store node could not rebuild: in 0.9.58's live check, Terminal-Lego and OpenSWE images built in sandboxes died with SIGBUS on their first command ("the store holds this chunk re-encoded"). Builder-built (`builder_format: rafs`) images had the same exposure.
  - The converter (sandbox builds and rafs builders) finds known chunks stored in another encoding, packs them again as nydus wrote them, and the index (`commit`'s new `supersede`) moves them to that copy; roots already registered keep their packs. Before signing, every chunk of a converted layer must be stored as its blob needs, or the build fails.
  - A reused layer whose chunks are misencoded is converted again.
  - The store node checks a stored blob layout against the blob's own chunk table and takes the index's current locations when they differ, so layouts written before the fix heal.

## 0.9.58 - 2026-10-08 (gateway)

- **Sandbox builds (C2.14, docs/sandbox-builds.md).** A recipe whose Dockerfile the prepared catalog splits into a chunk-store base and a remainder is built without Docker: a sandbox starts from the base's root on a worker, the remainder runs as one generated script, the worker's `commit-export` stages what changed, and only that layer is converted and stacked on the base (`RafsConverter.extend`, `nydus-image merge --parent-bootstrap`). No foundation copy, no Docker push or pull, no reconversion, no builder VM. Recipes it cannot run (multi-stage, `ARG`, `ADD`, a private `COPY --from`) still go to the builders.
  - Docker's rules for `RUN`, `COPY` (and `COPY --from` a public image, pulled on the gateway host), `ENV`, `WORKDIR`, `USER`, `SHELL`, `CMD` and `ENTRYPOINT`; build caches are kept as Docker keeps them.
  - The images are born in the chunk store: an `image_roots` row `released` from the start (wave `sandbox-build`) and a gateway record named by the manifest they would have had.
  - `ucloud-sandbox-builds.service` (`serve-sandbox-builds`) runs them on the gateway host and alone holds the chunk store's S3 key there; the gateway hands it jobs through a spool directory. Off unless `immutable_environments.sandbox_builds.enabled`.
- **Commit exports with more than one deletion no longer fail the filter.** runsc writes every whiteout after the first as a hard link to the first whiteout device; the filter refused those links (`commit_residue_forbidden`), so any commit deleting two paths failed. They are whiteouts now.
- **`CommitPolicy.keep_build_residue`**: sandbox builds keep pip, uv and npm caches and apt archives; agent commits drop them as before (their policies and hashes are unchanged).

## 0.9.57 - 2026-10-07 (gateway; store node)

- **Build package cache on the store node** (`chunk_store.store_node.package_cache`, `ucloud-package-cache`). Canonical's archive times out from UCloud, so every Ubuntu-based sandbox build stalled in `apt` (the sandbox-build pilot's slow tail: 502 s for five small packages). An HTTP proxy for allowlisted package hosts, each served by a configured upstream (Ubuntu's by `mirrors.dotsrc.org`); package files kept for good (LRU past `max_bytes`), index files for `index_seconds` with a stale copy on upstream failure, one fetch per file at a time. Sandboxes may reach it (`sandbox.direct_network_allow_tcp`); build steps use it as `http_proxy`.

## 0.9.56 - 2026-10-07 (gateway; builders)

- **Builds end in the chunk store (`immutable_environments.builder_format: "rafs"`, on in production).** A build's last step, where the EROFS publisher ran, converts the pushed image into the chunk store; its tag names a copy annotated with the chunk-store root, so resolution and dispatch are unchanged.
  - Layers another build converted are reused through the index's layer claims: a foundation is converted once (93 s for TMax's largest), later task images about 20 s each.
  - Builders get the chunk_store block, the index write token and the S3 key (root-only, as the store node), and install the builder bundle's `nydus-image`, checked against the new `chunk_store.nydus_image_sha256`.
- **Recipe images lose their OCI copy once built into the chunk store.** Ensure records each one's `image_roots` row (one root, wave `recipe`) and deletes its manifest through `release_oci`'s fences; a lease or route defers it to a later call. A recipe rebuilds its image, so no regeneration receipt. Pinned recipes are an OCI-free owner. The registry no longer keeps 80-220 MB per task image; the chunk store adds about 5-15 MB (the build pilot).
- **Importer** (`scripts/import_image_recipes.py`): exports a pinned TMax or Terminal-Lego checkout's recipes (whole `environment/` trees, LFS objects resolved and checked) and registers them, optionally building them N at a time.

## 0.9.55 - 2026-10-07 (gateway)

- **Image recipes submit builds without waiting for them.** The recipe build payload lacked `wait: false`, so a builder ran each submission synchronously and one ensure call held its request through nine builds (530 s) in the first live check.

## 0.9.54 - 2026-10-07 (gateway)

- **Image recipes (C2.7, docs/image-recipes.md).** Training names images and never sends recipes; the gateway resolved only images prepared as sources, so a foundation-backed task's name had nothing to resolve to. Now:
  - `POST /v1/image-recipes` registers names with their build (an uploaded context, Dockerfile path, build arguments) and a retention class, `pinned` or `cached`. Identical recipes share one image, `recipe-<sha40>`; contexts are kept past the upload store's one-day expiry.
  - `POST /v1/images/ensure` reports each name (`ready` with its reference, `building`, `queued`, `failed`, `unknown`) and submits missing builds through the ordinary build dispatch, at most 32 per call. Build state is the gateway's own, since builders forget theirs.
  - A create naming a registered recipe gets the built image, or a retryable 503 `image_building` that starts its build.
  - SDK 0.4.37: `ImageRecipe`, `register_image_recipes`, `ensure_images`, `wait_for_images`.
- `/v1/images/build`'s dispatch is a method that returns its answer, shared with recipes; its responses are unchanged.

## 0.9.53 - 2026-10-07 (gateway)

- **Base regeneration retries a store node's transient errors.** A store node answers 503 when its S3 fill misses a deadline (a cold cache under load). Regeneration took the first such answer as final; in the C2.14 build pilot one failed TMax's largest foundation and, through the 600 s failure backoff, 45 of 100 builds. Regeneration and unpack now retry 502/503/504 and connection errors for about a minute; a 4xx or unverifiable bytes still fail at once. Workers' reads are unchanged.

## 0.9.52 - 2026-10-07 (gateway)

- **Released build bases regenerate again.** Stored chunk locators keep the store node URL of the day they were built, and production's still named the Hetzner store node (10.42.0.200), so since the move to UCloud every regeneration failed ("chunk locator names a source other than the configured store node"), and with it every build from a released foundation: all of OpenSWE, TMax and Terminal-Lego's foundation-backed tasks. The Python chunk reader now takes only the object path from a locator and reads it on its configured store node; anything that is not a store object path is still refused. Workers were unaffected (nydusd names objects by key). Found by the C2.14 build pilot.

## 0.9.51 - 2026-10-07 (gateway; workers)

- **Toolkit layers (C2.5, docs/toolkit-layers.md).** A sandbox can ask for read-only toolkits (`SandboxSpec.toolkits`, at most 4, `name:tag` or `name@sha256:<root>`). The gateway pins them and dispatches the image's root with the toolkits' components on top, signed with the deployment's environment key; nodes mount and share it like any root. Files live under `/opt/ucloud/toolkits/<name>`; nothing in the sandbox's environment changes.
  - `toolkit-register` / `toolkit-list` manage `name:tag` in `toolkits.sqlite3`; retention keeps registered toolkits and every composition.
  - A retry keeps the toolkits its route pinned. Committed images and toolkits that are not one whole-image component are refused.
  - Specs without toolkits are unchanged, fingerprints included.
- **The verifiers harness toolkit build** (`runtime/toolkits/vf-harness`): uv, a managed Python and each harness program's prebuilt environment. In a fresh image with no network, verifiers' preparation takes 110 ms (12 s per rollout in the 2026-10-06 dry run, installing from PyPI).

## 0.9.50 - 2026-10-06 (gateway; workers)

- **Workspaces format without a whole-device discard (`mkfs.xfs -K`).** Format was 80% of `PrepareVolume` in the two-worker rehearsal (mean 350 ms, p95 956 ms; 10 ms on a loop device): a discard of the whole virtual size through ublk, on an upper that is fresh and reads as zeros anyway.
- **Workers with a slow boot CPU check retire.** VMs of one UCloud product measured 1-3x apart in single-thread speed with no steal reported, and a slow pair turned a 37 s warm 1,024-rollout start into 68-92 s.
  - Each agent times a fixed loop once at start (`cpu_check_ms` in its runtime metrics; about 22 ms on a good worker).
  - `policy.max_cpu_check_ms` (0 is off; production 35) retires a node over it as repeated reboots do: no capacity, the soft-drain slot, a stop once idle.
- **The daemon times the agent's admit and finish per create** (`agent_admit_ms`, `agent_finish_ms`): p50 64 and 93 ms on a good pair, about 5% of a create.

## 0.9.49 - 2026-10-06 (gateway; workers)

- **Exec starts per node: 120-280/s to 1,239-1,306/s** (p50 24-25 ms through the gateway; M2's gate is 1,000/s). Two fixes in `ucloud-noded`, found by per-check timings now in the exec start reply:
  - Every exec read the drain row in its own SQLite transaction. Concurrent WAL readers backed off for up to seconds (p95 3.5 s at 128 in flight), and readers past the 64 pooled connections leaked descriptors. Execs now share one read that began after they arrived, so a committed drain still closes admission.
  - A `pre_exec` hook (for a parent-death signal) made every exec a full `fork()` of the daemon on one of four threads, under its mmap lock: 74 ms of CPU per exec. Execs now use `posix_spawn`; children still die with the daemon through the unit's `KillMode=control-group`, as under the Python agent.
- **The environment backend measures its device cache only on attach.** Every create ensures each component of its image, nearly always already attached, and every ensure stat-ed every blob in nydusd's cache: `image_lease` p95 2.6-6.5 s in two-worker bursts.
- **The package and suite line budgets are gone.**

## 0.9.48 - 2026-10-06 (gateway; workers)

- **Images stay cached between sandboxes.** On every delete the environment store collected an image no sandbox referenced any more: it unmounted the composition and dropped its components. A rollout therefore kept re-materializing the same images, and `image_resolve` reached p50 0.5 s and p95 3.6 s in the 0.9.47 warm rehearsal (0.9.43, with Python creates: 0.33 s).
  - Unused compositions now stay mounted. A sweep evicts the least recently used only above `immutable_environments.device_budget_percent` (default 75% of the NBD devices) or the backend's cache budget, and immediately on device exhaustion.
  - The daemon makes one materialize request per image burst instead of one per create.
- **Sandbox networking over netlink, with a prepared-pair pool in `ucloud-noded`.**
  - No more `ip` processes.
  - The pool of 32 pairs that Python's agent used to keep (off under Rust creates) now lives in the daemon.
  - Creates share one durable write of the network state, and cold namespace creation is serialized.
  - Locally, at 32 concurrent creates, p50 went from 113 ms to 16 ms pooled and 38 ms cold.
- **IPv6 is off on sandbox veths.** Nothing in the sandbox contract uses it, and it reached host services on `[::]` past the IPv4-only INPUT rules. The initial pool fill still leaves it on (follow-up).
- **Measured (warm 1,024-rollout rehearsal):**
  - Ready by 30 s: 895 of 1,024 (0.9.47: 694; 0.9.43: 610). All ready by 37.3 s.
  - Ready p50 15.9 s.
  - Answer→resume p95 34 ms.
  - One create in 1,024 materialized an image.

## 0.9.47 - 2026-10-06 (gateway; workers)

- **The create path's storage step.** 0.9.45's 64-slot rehearsal put `storage_prepare` at p95 3.5 s.
  - **The storage daemon** admitted every lifecycle operation (creates, parks, wakes, seals) through 8 node-wide slots. New-workspace prepares now have their own admission class, bounded by `sandbox.storage_native_max_concurrent_prepares` (default: the node's create concurrency), so parks cannot starve creates and creates cannot starve wakes.
  - **Pool and process spawns:** the ublk device pool keeps 64 idle devices (was 2). Mounts and readahead are direct syscalls instead of processes, and a create no longer lists every device owner.
  - **Memory-backing prepare (in `ucloud-noded`)** sets XFS project ids and limits with ioctl and quotactl instead of two `xfs_quota` processes, and group-commits its journal. Locally, at 32 concurrent prepares, p50 dropped from 288 to 23 ms.
  - **Diagnostics:** spans on every create step and queue-wait metrics in GetMetrics. noded splits `storage_prepare` into `memory_prepare`, `volume_prepare` and `disk_claim`.
- 0.9.46's rehearsal: answer→resume p95 0.19 s cold (0.9.45: 3.3 s) with the pause tier in the daemon.
- The gateway upgrade sets the pool watermarks to 64/64.

## 0.9.46 - 2026-10-06 (gateway; workers)

- **Phase 3a: the pause tier runs in `ucloud-noded`**, behind `sandbox.direct_node_rust_pause`. It requires the pause tier, Rust creates and Rust execs.
  - The daemon now owns pause and thaw, thaw-on-exec, idle pause, local model waits (nft and NFLOG), reclaim and the escalation decision.
  - Python keeps escalation execution (`/internal/v1/pauses/escalate`), growth bookkeeping (`/internal/v1/growth/events`), relay parks, wake, park and delete.
  - Pause markers stay the truth. A flock on a marker means a thaw is in progress, and the exec fence's T and A locks order Python's transitions and ops against the daemon's pauses.
  - The heartbeat adds the daemon's `status.json` counters to its own and folds each daemon session into its totals.
  - Why: 0.9.45 left the answer→resume path in Python, sharing its GIL with every create's admit and finish (answer→resume p95 1.4 s warm). In relay rollouts every exec met a paused sandbox, so phase 2a forwarded them all.
- Rust execs now require Rust creates. The agent's own operations wait at most the admission wait for a transition, then defer.
- The gateway upgrade turns on `sandbox.direct_node_rust_pause`.

## 0.9.45 - 2026-10-06 (gateway; workers)

- **Rust creates, fixed from the 0.9.44 canary.** Two defects stopped every create the daemon ran:
  - it read the storage daemon's volume record as nested (`owner`), but the record is flat, so every create failed with "storage-native volume record does not match its workspace" (503, ambiguous);
  - `libc::getrandom` in a static musl link resolved to address 0, so the first overlay prepare crashed the daemon (SIGSEGV; found from a core dump against the same commit's debug build). Random bytes now come from the raw syscall, and `build_pinned.sh` refuses a binary in which any libc function the source calls is not linked.
  - On a hot-swapped worker, 60 of 64 relay rollouts succeeded (the 4 failures are images without Python). Ready p50 was 3.7 s and p95 5.4 s, with `runtime_create` p50 205 ms and `storage_prepare` 291 ms.
- **Phase 2a: execs on running sandboxes run in `ucloud-noded`**, behind `sandbox.direct_node_rust_exec` (requires the front door and Rust creates).
  - The daemon starts `runsc exec` for owned, running, unpaused sandboxes and serves the sessions it started (events, stdin, signal). Python's session semantics are ported exactly, against Python goldens.
  - Python's lifecycle transitions and the daemon's execs exclude each other through two flock files per sandbox (`.<id>.transition`, `.<id>.activity`). A's mtime is the shared activity clock for idle park. There is no per-exec RPC.
  - Everything else stays with the agent: paused or parked sandboxes, tty, drain, memory pressure, unknown sessions, files and jobs.
- The gateway upgrade turns on both flags.

## 0.9.44 - 2026-10-06 (gateway; workers)

- **Phase 1 of the Rust node daemon: creates run in `ucloud-noded`** (docs/rust-node-daemon-plan.md), behind `sandbox.direct_node_rust_create` (requires the front door).
  - The daemon owns the node registry (`--registry-foreign` on the agent) and runs the create pipeline end to end: image lease, plan, split memory allocation and workspace volume, direct network, OCI config, overlay rootfs, guest files and init, `runsc create`/`start`, the lifecycle journal, `owned`. Each phase resumes on an exact replay.
  - Python keeps admission, drain and the lifecycle lock. The daemon asks the agent to admit (validation, startup slot, capacity, lock) and to finish (the record, epochs, post-create bookkeeping) over the agent's socket, so heartbeats, drain and the create response are unchanged.
  - Anything the daemon does not fully support goes to the agent unchanged: relay egress, the static management helper, DNS-named egress, the legacy image store, specs it reads differently, and every framing or validation edge.
  - Fixed while porting: a failed `runsc create` now deletes what runsc left, and a runtime without a journal is deleted.
  - The gateway upgrade turns the flag on.

## 0.9.43 - 2026-10-06 (workers; gateway renders node init)

- **Phase 0 of the Rust node daemon** (docs/rust-node-daemon-plan.md). `runtime/noded` (`ucloud-noded`, Rust: tokio and hyper) can own the node's TCP port and forward every request to the Python agent, now able to serve on a Unix socket (`--unix-socket`).
  - One upstream connection per client connection, reused across keep-alive.
  - Both bodies stream unbuffered.
  - An unreachable agent is a retryable 503 `node_agent_unavailable`; a failure after the request was written closes the client connection without a response.
  - The binary is a reproducible static musl build pinned in the sandbox bundle (`runtime/noded`).
  - Init installs and runs it only with `sandbox.direct_node_front_door` (default off). Nothing changes until it is set.

## 0.9.42 - 2026-10-06 (workers)

- **Sandbox veths get one queue pair, not one per CPU.** veth allocates a TX and an RX queue per possible CPU by default, and each queue is a sysfs object with its own add and remove uevents. On a 64-vCPU worker, one sandbox's pair produced 280 uevents (create, up, delete), against 31 with `numtxqueues`/`numrxqueues` 1 on both ends. In a 1,024-rollout burst, D-state kernel stacks showed udev workers blocked on the sysfs root's lock (`kernfs_dop_revalidate`) about 3,250 times per worker, against a few dozen for anything else. That lock also serializes every device add on the create path. gVisor's netstack reads eth0 through one packet channel.

## 0.9.41 - 2026-10-06 (gateway renders node init; workers and builders)

- **udev neither probes nor watches nbd and ublk devices.** A worker creating about 500 sandboxes ran 144 udev workers on 6–10 of its 64 cores. systemd's persistent-storage rules blkid-probe every nbd and ublk device (1,024 + ~500), and the block watch re-probes each one after every close-for-write, such as the storage daemon's mkfs. That mkfs also waits on udev's device lock. Init now installs rules setting `UDEV_DISABLE_PERSISTENT_STORAGE_RULES_FLAG` and `nowatch` for these devices before nbd loads, and masks udisks2 (a desktop service, 0.5 cores).

## 0.9.40 - 2026-10-06 (workers)

- **The node registry's writers share group commits.** In a 1,024-rollout burst, 25–42 writers queued for the registry's one writer while each held it through its own durable COMMIT. Creates waited about 1.7 s for the writer (`registry_turn_ms`, 0.9.39), while the COMMIT itself took 5 ms at the median. State-disk fsync under the burst spiked to p95 60–110 ms and up to 565 ms, and every queued writer waited out each spike. Queued writers now run under SAVEPOINTs of one transaction, and the last in line (none queued, or 64 served) commits for all. A failed writer undoes only its savepoint. The owner's commits are now always durable: `commit_owned`'s unsynced commit gives way to a shared fsync.

## 0.9.39 - 2026-10-06 (workers)

- **Create timings split the registry's time.** `registry_turn_ms` (waiting for the one writer) and `registry_sync_ms` (the commit) are reported beside `registry_commit_ms`, which was 0.7–1.3 s per create in bursts for about 2 ms of SQL.

## 0.9.38 - 2026-10-06 (workers)

- **A managed-process create is not charged the memory bound its launch carries.** A managed sandbox runs no workload until its launch, which keeps its whole bound until its first safe wait. Charging the bound at create as well counted it twice. With 64 startup slots, a 1,024-rollout burst's creates waited in memory admission for a mean 3.0 s (p95 16 s), on workers that never fell below 141 GB of 192 GB available. Live memory pressure and the shape check still gate each create. Other sandboxes keep the bound at create.
- **Node-local waits and resumes commit to the registry in batches.** Each was its own durable transaction, tens per second per worker during rollouts, competing with creates for the registry's one writer. The bookkeeping thread now commits everything queued since its last commit in one transaction, with a SAVEPOINT per item so a superseded wait fails alone.

## 0.9.37 - 2026-10-05 (gateway and workers)

Create throughput, from traces and `py-spy --gil` of the 0.9.35 and 0.9.36 1,024-rollout rehearsals. Creates held a node startup slot about 3.4 s (p50) for well under 1 s of work, which bounded two workers near 19 creates/s.
- **Growth forecasts are cached per owner.** Every admission (create, exec start, wake, launch) recomputed every active sandbox's forecast and re-summed the ledger under the node-wide capacity guard: about a fifth of the node agent's GIL in a burst. An owner's forecast now changes with its intent, all of them once per sampler pass, and in admission only when the oldest credited sample passes the 30 s credit age. The ledger keeps running totals, so a projection costs O(in-flight claims).
- **Pool interface checks no longer run modprobe.** `if_nametoindex` on a not-yet-created interface makes the kernel call `request_module` twice as root, with the GIL held (6% of it). sysfs answers instead.
- **Virtual block devices are skipped before resolving them.** The once-a-second disk sampler resolved all 1,024 nbd devices and every ublk and loop device (5.6% of the GIL).
- **The gateway's registry-lease lock is per owner.** One host-wide key serialized every create's lease I/O across the six API processes: p50 0.56 s and p90 4.1 s per create.
- **Soft-drain waits out recent creates.** The autoscaler drained one of two workers four minutes into a rollout step, because by memory claims everything fit on one. It stopped once idle, and the next step's burst ran on one worker while a replacement provisioned. A busy worker is now selected only after no create has been placed for the effective `scale_down_idle_seconds`.

## 0.9.36 - 2026-10-05 (gateway and workers)

- **Local-wait growth bookkeeping is queued off the pause and thaw paths.** 0.9.35 recorded each local wait's forecast inline: a durable registry write under the sandbox's lifecycle lock, on the wait scheduler's threads. In a create burst the registry's one writer is busy. Thaws queued behind those pauses past the relay's 5 s local-wake grace, and 571 of 7,872 answers took about 10 s through the gateway wake (none before 0.9.35). One background thread now records waits and resumes in order.

## 0.9.35 - 2026-10-05 (gateway and workers)

- **Node-local model waits suspend and resume growth forecasts.** Growth bookkeeping saw only gateway relay parks. Node-local waits, which pause most waits in production, left every agent charged its launch's whole memory bound. A 192 GB worker then stopped admitting launches at about 160 sandboxes with about 160 GB free, and agent starts waited out 30 s deadlines (37 of the 0.9.34 rehearsal's failures). A local-wait pause now records the wait, suspending the forecast, and its thaw records the continuation, forecast back to its demonstrated peak. The thaw itself is not gated.

## 0.9.34 - 2026-10-05 (gateway, workers and builders)

- **UCloud workers are bootstrapped over the private network.** A worker that shares the control plane's private network is reached as `ssh ucloud@<hostname>` there. UCloud's public SSH proxy, announced in the job's status, is the fallback. In a 1,024-rollout rehearsal the proxy reset and refused a fresh worker for minutes while its private address answered at once, so the first wave ran on one worker.
- **Paused waits are swapped out for growth forecasts only after a 1 s settle, one at a time.** The pause tier's reclaim tick billed growth forecasts (guarantees, not allocated pages) as pressure. In the rehearsal a launch burst's forecasts swapped out 393 paused waits (31.6 GB) on a worker with 155 GB free, and their thaws then prefetched for 0.76 s each, making relay overhead p95 3.0 s. Measured pressure still reclaims in parallel.
- **A managed launch no longer takes a startup slot.** Launching the primary process of a running sandbox queued FIFO behind cold creates for the node's 8 startup slots. In the burst, 315 agent starts waited out the 30 s deadline behind hundreds of creates until the client gave up. The launch's memory guard still bounds it.

## 0.9.33 - 2026-10-05 (workers and builders; gateway only carries the release)

- **The node bundles' kernel module closure carries `erofs`, `nbd`, `nft_log` and `nfnetlink_log`**, the modules 0.9.32's init loads. Init requires the bundle's load list to equal `RUNTIME_KERNEL_MODULES`, and the 0.9.32 repacks kept the 0.9.6 list, so every 0.9.32 worker and builder stopped at "bundled kernel module load list does not match this runtime". (0.9.32's note that the host's modules would do was wrong.) The four files come from `linux-modules-7.0.0-30-generic` 7.0.0-30.30, the build whose other 25 files the bundle already pins, byte for byte.
- **nbd's device pool holds for init's first load.** Environment bootstrap loads `nbd nbds_max=1024 max_part=0` only when nbd is not loaded yet, and init now loads it earlier, which would have left a worker 16 devices and so 16 mounted components. Init writes the options to `/etc/modprobe.d/ucloud-sandboxes-nbd.conf` before loading any module.

## 0.9.32 - 2026-10-05 (gateway and workers; production on UCloud)

- **The sandbox bundle carries `ucloud-chunk-serve` again** (`runtime/chunk_serve`, the pinned 0.9.13 build, sha256 a8ff310b…). Releases 0.9.14 to 0.9.31 repacked the 0.9.6 bundles, which predate it, so a fresh store node could not be initialized from the current release; Hetzner's store node had been initialized once, at 0.9.13.
- **Store init reads `ucloud-chunk-serve`'s digest through `$SUDO`.** The bundle unpacks into a root-only directory. Hetzner's init logs in as root; UCloud's logs in as `ucloud`, and the pin check failed with Permission denied.
- **Builders bundle erofs-utils** (1.9.1, with the 21 libraries of its closure the bundle lacked, and curl at the bundled libcurl's version so offline APT upgrades it instead of removing it). Immutable builders publish EROFS images with `mkfs.erofs`; Hetzner's builder image shipped it, UCloud's vm-ubuntu image does not, and builder init stopped at "immutable builder requires bundled erofs-utils". `BUILDER_RUNTIME_PACKAGES` names it, so the builder bundle's package list does too.
- **`scripts/rehome_registry_host.py`** rewrites the managed registry's authority in gateway state after the gateway moves: image references are stored as `<gateway_private_host>:<registry_port>/ucloud-managed/...` in the image, prepared-image and registry-usage databases and in PostgreSQL (group specs, parked sandboxes' storage snapshots). Dry run by default; `--execute` refuses while gateway services run. Moving production to UCloud rewrote about 47,000 references from `10.42.0.2:5000`. Clients that stored prepared references (the training selection's `prepared_reference`) need the same rewrite; ones that send the task's source image do not.
- **Worker init loads `erofs`, `nbd`, `nft_log` and `nfnetlink_log`** with the other runtime modules (immutable environments, RAFS over NBD, node-local model waits). Not deployable as released: the repacked bundles kept the 0.9.6 module load list, which init must match exactly; 0.9.33 adds the modules to the closure.

## 0.9.31 - 2026-10-05 (workers)

- **The sandbox bundle carries a managed init built from this repository** (`runtime/managed_process`, commit a8541f3, Go 1.27.1, sha256 785c16f9…), replacing the 0.9.6 bundle's binary (cedddb16…, whose source and toolchain are not recorded). It adds `files extract`, so managed-process and static-helper sandboxes created on these workers extract archive uploads with one exec instead of answering 501. Every other native file in the bundle is unchanged.

## 0.9.30 - 2026-10-05 (gateway and workers)

- **This release repacks the Python agent only:** the node bundles keep the 0.9.6 managed init binary, so the Go `files extract` helper below is not deployed yet. Shell-helper sandboxes (the default; `tar` in the image) extract archives; managed and static-helper sandboxes answer 501 `archive_upload_unsupported`, and SDK 0.4.36 falls back to per-file uploads.
- **`PUT /v1/sandboxes/{id}/archive?path=DIR` writes many files with one exec.** Each single-file upload is a helper exec in the sandbox, so a 40-file harness costs 40 execs per sandbox; at 0.9.28 that pattern ran near 69 files/s per worker, about 100 s for a 512-rollout burst. The body is a tar, plain or gzip (detected by its magic), within the file upload's 256 MiB limit; its files may total 256 MiB and it may have 10,000 members.
  - The worker validates the whole archive before anything is written. Only regular files and directories are accepted. Names must be relative, without `..` or control characters (a leading `./` is fine). Links, devices, FIFOs, sparse files, duplicate paths and a file that is also a directory answer 400.
  - The helper never parses the client's bytes: the worker passes it a fresh plain tar of the regular files and names empty directories as arguments. Static-helper and managed sandboxes run `/.ucloud-job-init files extract DIR MAX [DIR...]`, which replaces each file atomically. Shell-helper sandboxes run the image's `tar -x -m -o` under `umask 022`, which replaces files in place. Either way a file gets its member's permission bits (setuid, setgid and sticky dropped) and the exec identity as owner. Missing parents are created; existing directories keep their modes. The archive is not atomic as a whole: a failed extraction can leave some files written.
  - The upload takes the same path as a fenced file write: the generation check, then the lifecycle shared fence, then a fenced exec. A sandbox replaced while the body arrived answers 409, and a parked one wakes.
  - It answers 501 `archive_upload_unsupported` when extraction is impossible, and the SDK then uploads the files one by one. This happens when the owner's heartbeat lacks `sandbox-archive-upload-v1` (the gateway checks), when a shell-helper image has no `tar`, or when a sandbox's static helper predates `files extract`. The helper is installed in the rootfs at create, so sandboxes created before the deploy keep the old helper.
  - SDK 0.4.36 `upload_files` uses this endpoint. Deploy workers with the gateway (or before it).

## 0.9.29 - 2026-10-05 (gateway and workers)

- **A command's running check no longer waits for the sandbox's other exec starts.** It read the lifecycle journal under the warden's per-sandbox fence, which every streaming exec's lease holds until its `runsc exec` child starts (`manager.inspect` p50 331 ms in a 0.9.28 burst). Under the node's shared lifecycle fence no transition is in flight or can start, so it now reads the atomically replaced journal revision and checks the Sentry's identity without the warden fence.

## 0.9.28 - 2026-10-05 (gateway and workers)

- **A running sandbox's commands and file writes no longer take its request lock.** Exec admission held the per-sandbox lock to inspect the lifecycle journal, so one sandbox's commands queued behind each other's inspections (`lifecycle_ms` p50 612 ms at 128 concurrent execs, about 114 execs/s per node). File writes held it, without waiting, for the whole write, so concurrent writes to one sandbox were refused (`node_startup_busy`): the harness pattern, 40 small files per sandbox at once, ran at 5.6 files/s per node. Both now run under the lifecycle coordinator's shared fence, which already excludes park and delete; the request lock remains for a sandbox that must be reconciled or restored, where it waits instead of refusing for exec. An upload still checks its sandbox generation first and answers a conflict when it was replaced.

## 0.9.27 - 2026-10-05 (gateway and workers)

- **Delivered exec results free their session slot first.** A node keeps at most 1,024 exec sessions and held each finished one for 30 s, so short commands capped a node near 34 execs/s; the rest answered 503 `node_active_exec_deferred`, which the SDK retried with backoff (20-25 s stalls). A session whose start response carried its final event now goes at once at capacity (a lost start response leaves no session id to ask by); one whose final event an events poll returned goes after a 2 s grace for a re-poll. Other finished sessions keep the 30 s retention.
- **`chunk-migrate release-originals [--execute]`** deletes an OCI-released image's untagged manifests of the same config (the build output before the environment annotation moved its tags), fenced by leases.

## 0.9.26 - 2026-10-05 (gateway only)

- **Power-of-k choices hold their workers at once.** A create, and every member of a group, is charged to this process's overlay when it is chosen, under a per-process planning lock with no I/O, until its own reservation (or failure) replaces the hold. Before, the charge came after the intent's database write, so a burst's concurrent choices all saw the same idle-looking worker: in a 64 x 8 group burst, `pack` put 504 of 512 sandboxes on one of three workers.

## 0.9.25 - 2026-10-05 (gateway only)

- **A regenerated base matches its receipt on any host.** `nydus-image unpack` names each tar entry's owner and group from the host's `/etc/passwd` and `/etc/group`, and uid 100 is `postgres` on the gateway but unnamed on the M2 converters that wrote the receipts. So the gateway's regeneration missed the verified diff ID, and the first build on an OCI-released base answered 409 `base_released`. Regenerations now rewrite each name the host looked up to the converters' pinned table, in place, fixing only those header fields and their checksums.

## 0.9.24 - 2026-10-05 (gateway only)

- **A queued group's 503 that created members finishes its command** (`X-UCloud-Group-Delivered`). Only the answer that created members carries their records; requeueing it held running members idle until the whole group was placed or its deadline passed. The client starts them and repeats the request, which places only unplaced members. A 503 that placed none still requeues.

## 0.9.23 - 2026-10-05 (gateway and workers)

- **`chunk-migrate drop-staged --kind upstream|shared [--execute]`** deletes the staged upstream sources (`ucloud-upstream`) and shared-task sources (`ucloud-shared-sources`), which nothing reads once their prepared images exist. Each digest's staging leases go first, then its manifest is deleted in the usage store's writer transaction unless any other lease holds it. `release-oci` closes its routing store (no PostgreSQL pool traceback at exit).
- **The upgrade runs the routing schema's additive migration**, which C3.2 groups need on PostgreSQL (`sandbox_groups`, `sandbox_group_members`, the `group` command kind).
- **C4.3 phase 2: creates leave the loopback (only with `gateway_create_placement: "power_of_k"`, which is off).** Each API process claims queued creates and groups from `gateway_commands` and runs them on its own handler, from memory instead of a loopback HTTP request, under the same claim token, lease, command fence and 503 requeue. The placement service then claims wakes only. Each API process keeps up to `max(32, ⌈create_target_concurrency_per_node × max_nodes / gateway_processes⌉)` creates in flight: 32 each, 192 in all, in production. Ranked mode is unchanged: API processes claim nothing and the placement service replays every kind to its loopback. The switch is read at process start, so flip it by restarting the gateway and placement units together (a deploy does); see `docs/c43-placement-wiring-plan.md`.

## 0.9.22 - 2026-10-05 (gateway, workers and builders)

- **M2 converters free the NBD devices a killed converter leaked.** Before each image, a slot disconnects its devices whose owning process is dead, after unmounting their EROFS mounts and the overlay stacked on them. Before, every later image on that slot failed with "no free NBD device".
- **Volume-free builds (M2 plan §5.4, not run).** A build that names an image whose OCI manifest was released gets that image's regenerated copy as a BuildKit named context (`--build-context <ref>=docker-image://ucloud-regenerated:…`); the context and Dockerfile bytes, catalog pins and decisions are unchanged. The copy is one layer with the verified tree and the original config (ONBUILD, SHELL, labels kept). The gateway regenerates a missing copy with `chunk-migrate regenerate` and answers `503 base_regenerating` (retryable) until it exists; `409 base_released` when it cannot. Off unless `immutable_environments.regenerate_bases` is set (needs `nydus-image` on the gateway).
  - `chunk-migrate verify-regeneration` (converter) writes a receipt per image: its regenerated tree equals its OCI layers, with the layer's diff ID and the config bytes. Regeneration must reproduce that diff ID.
  - `chunk-migrate release-oci --include-build-inputs --receipts …` also releases build inputs that have a receipt, recording it first. `chunk-migrate status --out` lists the rows to verify.
  - Builders and the gateway ship together: an older builder ignores `base_contexts` and such a build fails on the missing manifest.

## 0.9.21 - 2026-10-04 (gateway and workers)

- **The registry prune deletes 8 manifests per lease-fence transaction, not 64.** Each batch's registry DELETEs run inside one write transaction on the registry usage database. 64 held it for nearly a minute, so other writers (`chunk-migrate switch` and `release`, lease writes) timed out after 60 s, and a gateway start could not read it ("registry usage database is invalid or unavailable").
- **M2 OCI release readers** (see Unreleased notes below): a released image resolves by tag and by digest after its OCI manifest is deleted, and `chunk-migrate release-oci` deletes non-build-input released images' manifests. Workers take the pull's root. Not run yet.
- **C3.2 group create** (`POST/GET/DELETE /v1/sandboxes:batch`) ships with C4.3 phase 1. Both are active only with `gateway_create_placement: "power_of_k"`, which is off.

## 0.9.20 - 2026-10-04 (gateway only)

- **A create pinned to its image's dispatched chunk-store root skips the node's separate image pull.** The create attaches that root itself. The pull carried no root, so the worker resolved the image's annotation, which is the old EROFS root. Once `chunk-migrate release` drops that root's leases, the prune deletes it, and the first create of a released image on a fresh node would fail. It also attached the old EROFS root for nothing on every burst.
- **Ships C4.3 phase 1 switched off** (`gateway_create_placement: "ranked"`): power-of-k create placement, see `docs/c43-placement-wiring-plan.md`.
- **`chunk-migrate release-oci --wave N [--execute]`** (M2 plan §5.4) deletes the OCI manifest of each `released` image that is not a build input. It first remembers the image's tags in `image-roots.sqlite3`. Deletes are fenced in the usage store's writer transaction, as retention's are. A digest stays when it is leased by an owner other than catalog owners, routes and create pulls, or when a route without a pinned root, a prepared sandbox or a warmup names it. Without `--execute`, it only counts images and layer bytes.
- **A released image resolves without its manifest.** When the registry has no manifest, the gateway answers a digest from the image's `released` row, and a tag from its remembered digest. This covers creates by tag, digest and name, image listings, the dependency resolver, and the prune's stale build-record check. Pressure eviction skips dispatched images. A commit on a released parent is refused with `commit_parent_released`.
- **The gateway's per-node pull sends the dispatched root** (`environment_root`), and immutable-environment workers attach that root instead of reading the manifest's annotation.
  - Why: a worker's first pull of a released image attached its old root, which retention deletes after `release`.
  - Deploy workers with the gateway before running `release-oci --execute`.

## 0.9.19 - 2026-10-04 (gateway only)

- **`chunk-migrate release --wave N [--execute]`** (M2 step 4, EROFS only). For each switched image, it drops its durable owners' `:environment` leases on digests in the old closure that the new closure does not contain, and marks the row `released`. Retention already ignores a dispatched image's annotation, so its hourly prune then deletes the old roots and components nothing else keeps, and the registry sweep frees their bytes.
  - An owner's lease stays when another of its images still needs the digest, by that image's dispatched root or its annotation. If such an image cannot be resolved, all of that owner's leases stay.
  - The OCI manifest stays: readers for a deleted manifest are not built, and builds read `FROM` layers from it.
  - Without `--execute`, it only counts.

## 0.9.18 - 2026-10-04 (gateway and workers)

- **A nydusd attach loads only what nydusd reads.** The worker fetches the bootstrap and chunk map from the store node by the digests the signed component pins, with no index call. It verifies both digests, checks the chunk map's regions against the bootstrap's device table, and writes the bootstrap for nydusd. nydusd checks every chunk against the bootstrap itself.
  - Why: building the Python reader's per-chunk state took seconds per large image, and all of a node's attaches share env-io's one GIL. In a warm 512-rollout burst, env-io sat at 120-160% CPU and an attach took 8 s instead of 0.5 s. Traces showed 64% of create time was image preparation.

## 0.9.17 - 2026-10-04 (gateway only)

- **Awaited environment attaches are no longer capped at 32 per gateway process.** The cap (`MAX_BACKGROUND_CREATE_IMAGE_PULLS`) bounds pulls that outlive their 2 s callers. An awaited attach always has a waiting create, so creates in flight bound it. With 96 creates in flight (0.9.16), the cap turned away 579 creates in 10 s as `image_warmup_pending`.

## 0.9.16 - 2026-10-04 (gateway only)

- **The placement worker allows every node's startup slots in flight:** `create_target_concurrency_per_node × max_nodes` creates (96 in production), not a fixed 32. With 0.9.15's awaited attaches a create takes ~5-6 s, so 32 in flight capped a 3-node burst near 6 creates/s while nodes had 32 slots each. A stopgap on today's create path; C4.3 replaces the queue-side bound.

## 0.9.15 - 2026-10-04 (gateway only)

- **On immutable-environment workers, a create waits for its image's attach** (up to 30 s) instead of getting a 503 `image_warmup_pending` after 2 s.
  - There, the gateway's per-node "pull" is the attach (resolve and mount the components): seconds, not an OCI pull's minutes. Creates for one image on one node still share one attach.
  - Why: 0.9.14's deferral log showed it was nearly every retry in a 512-rollout burst. About 100-160 creates per 10 s went back to the durable queue and polled with backoff while their attach ran.
  - OCI workers keep the 2 s answer.

## 0.9.14 - 2026-10-04 (gateway only)

- **The placement worker says why it deferred commands.** Every 10 s with deferrals, it logs a warning counting them by kind, status, error code (or transport `code`) and the gist of the message, digits elided. A warm 512-rollout burst retried 182 creates up to 34 times, and nothing recorded the cause. `node_active_admission_deferred` covers both startup slots and memory, so the message is part of the key.

## 0.9.13 - 2026-10-04 (gateway and store node)

- **The store node's reads move to Go: `ucloud-chunk-serve`** (`runtime/chunk_serve`), enabled by pinning its sha256 in `store_node.native_server_sha256`.
  - Why: the Python node is bound by one GIL, near one core. In a warm 512-rollout burst it served ~1,400 small reads a second at ~15% of the CX43's CPU, and first commands waited 7-12 ms p50 (240 ms p99) to be sent.
  - It answers what it can from verified, resident extents: virtual nydusd blobs and plain objects, with the same statuses, headers and bytes. It hashes each extent on its first open, as the Python node does, never serves a torn one, and uses sendfile on all cores.
  - Everything else goes to the Python node on 127.0.0.1 at the same port: misses (it fills from S3), bad tokens, malformed or unsatisfiable ranges, warm jobs, residency, health. `/v1/metrics` returns the Python node's metrics with a `native` section beside them.
  - Store init extracts it from the bundle (`runtime/chunk_serve`, added by `repack_node_bundle.add_chunk_serve` from `build_pinned.sh`), refuses a binary that does not match the pin, and runs it as `ucloud-chunk-serve.service`.

## 0.9.12 - 2026-10-04 (store node only)

- **The store node times the reads it serves** (`serve` in `/v1/metrics`): the wait for a read thread, the read (fills, layout lookups, page faults) and the send, each as p50/p90/p99/max over recent requests. It used to time only its S3 fills, which hid where a warm burst's requests waited.

## 0.9.11 - 2026-10-04 (store node only)

- **A slow disk read no longer stalls the store node.**
  - The node sent response bytes with `sendfile` on its one event loop. When those bytes were not in the page cache, the loop waited on the disk, and every other request waited with it.
  - Local NVMe hid this. On the Volume (0.9.10), a cold read takes about 7 ms. Behind 32 cold readers, a hot 64 KiB read went from 1.3 to 88 ms p50, and a 512-rollout warm burst's first commands from 1.5 to 17 s p50 for SWE-smith.
  - Now a read thread faults each response's ranges into the page cache first, so the loop's `sendfile` only copies from memory. The on-loop fast path for cached objects is gone: every read takes the read pool.

## 0.9.10 - 2026-10-04 (gateway and store node)

- **The store node can keep its replica and index on a Volume** (`store_node.data_device`, a `/dev/disk/by-id/` path).
  - Store init mounts the device at `/mnt/store-replica`, bind-mounts `cache_dir` and the index directory from it, and makes both services require those mounts. It never formats: the device must already be ext4.
  - Why: wave 3 (mostly ScaleSWE) dedups less than waves 1 and 2, and the replica would outgrow the CCX43's 360 GB disk. A Volume grows on its own and outlives the server, so the node can be a small, cheap type and be replaced without refilling from S3.
  - Off by default, and not rendered when unset, so older releases still read the block.

## 0.9.9 - 2026-10-04 (gateway and store node)

- **The chunk index keeps stored locators in its own database.**
  - A worker's first attach of an image reads that image's locator. The index used to fetch it from S3 on a cold request, and S3 stalled it for 56 s once in M2 wave 2.
  - Now the index reads its database first, falls back to S3 once (for a fresh index), and writes both when it builds a locator.
- **`chunk-migrate switch` verifies the whole closure before switching an image.**
  - An image with a missing component goes to `not_warm` instead of being half-switched.
  - Why: one wave 2 image had lost a component to the retention incident.

## 0.9.8 - 2026-10-04 (gateway and store node)

- **The store node is a full replica of its S3 prefix** (`store_node.replica`).
  - S3 is the permanent store, and the deduplicated corpus is small: 76 GB after waves 1 and 2, about 150–220 GB projected.
  - The node never evicts. A fill past `cache_bytes` is refused and counted (`full_refusals`), so the operator grows the disk.
  - A mirror lists the prefix every `mirror_seconds` and fills whatever is not resident. A fresh node refills itself the same way.
  - `POST /v1/resident` returns the keys that are not wholly resident.
- **`chunk-migrate switch` works in batches.** Per batch of 100 images it makes one residency check, one fill of whatever is missing, and one recheck. Before, it warmed about 3 s per image.
- **Removed:** 0.9.4 and 0.9.5's first-to-evict placement of builder reads and `keep=false` warms. They existed only to protect a warm set in an undersized cache.

## 0.9.7 - 2026-10-04 (gateway only)

- **Retention gives a converter's roots 72 hours to be recorded.**
  - Chunk-store tags (`rafs-root-*`, `rafs-*`) younger than the window are no longer deleted as unreferenced.
  - Why: wave 2's converted roots waited hours before `chunk-migrate record`, and the one-hour grace deleted 1,115 of them. The old roots and S3 chunks were untouched.
- **The registry prune no longer crashes.**
  - Since 0.8.6, `manifest_document` passed `timeout_seconds` to the build cache's `_json_request` override, which did not accept it.
  - As a result, every hourly prune failed in its build-cache pass.

## 0.9.6 - 2026-10-04

- **env-io runs with `LimitNOFILE=65536`.** A 512-rollout burst on 0.9.4 lost creates to EMFILE, because env-io sat at systemd's default of 1024. At about 140 attached images per node it held roughly 600 NBD sockets and 355 device fds. The node agent already ran with 65536.

## 0.9.5 - 2026-10-04 (store node and converters)

- **A converter's verification no longer warms hot.**
  - Warm requests take `keep: false`: extents fill first-to-evict and nothing is promoted. The converter's mount verifier uses it.
  - Why: the verifier warms each image's objects before mounting it, so M2 wave 2 would have evicted wave 1's warm set.
- **`chunk-migrate convert --shard I/N`:** converters split a wave, each image once.

## 0.9.4 - 2026-10-04

- **A converter's store-node reads never evict the warm set.**
  - Write-token demand reads put their extents at the eviction end and never promote existing ones. Restarts keep that order through mtime.
  - Warm jobs promote what they find.
  - Why: M2 waves are larger than the store node's cache, and converters verify by reading through it.
- **Only a blob's first nydusd starts alone.**
  - Each blob has a lock, held only until the first daemon on it is ready. Daemons on prepared blobs need no lock.
  - Before, every daemon start on a node took one lock, and 20 RAFS creates on a node queued about 14 s.
- **`chunk-migrate switch` skips an image whose warm call fails,** instead of stopping.

## 0.9.3 - 2026-10-04

What M2 wave 1's post-switch canaries found (wave 1 was reverted until this ships):
- **A stalled read is latency, not EIO.**
  - nydusd sets a fixed 60 s NBD timeout, and each expiry marks one of the device's connections dead for good.
  - Hetzner S3 stalls about 2% of GETs for 6–60 s, often again on retry of the same key.
  - Now the device timeout is 600 s, set after nydusd connects, and nydusd retries the store node for up to 270 s. Each retry joins the node's in-flight fill.
- **Warming covers everything workers read.** `warm-chunk-store` and M2 switches include every nydusd blob's tail and layout. Workers had fetched these from S3 at each first attach.
- **`chunk-migrate switch` dispatches only fully warm images.** It warms each image's objects first and leaves any image with a failed object unswitched (`not_warm`).
- **RAFS attaches never queue behind the EROFS attach limit.** They get a fixed 8, while production keeps EROFS serial. Serial nydusd attaches had cost a 20-sandbox burst about 30 s.

## 0.9.2 - 2026-10-04 (store node only)

- **Worker fills never wait behind a builder's.**
  - On the store node, requests with the write token (converters verifying through the node, and warm jobs) fill from their own pool of half the S3 concurrency, unhedged.
  - Worker fills (the read token) always have the other half.
  - Why: M2 wave 1's post-switch canary lost one sandbox to EIO. While the converter verified 20 large images through the node, a worker's fill missed the 60 s deadline and its NBD read timed out.
- **M1 gate:** the baseline worker drops production's chunk store and root dispatch.

## 0.9.1 - 2026-10-03 (store node and converters only)

- **Chunk-index registration survives S3's tail.**
  - The index now range-reads only each blob tail's chunk table, not the whole tail object with nydus's metadata.
  - Its presigned S3 reads retry transport errors and 5xx at 0.5, 1 and 2 s.
  - The index client waits up to 600 s for a registration.
  - Why: M1 gate run 4 lost 2 of 157 conversions and production wave 1 lost 4 of 53, to one 30 s S3 read timeout each on the register or commit path.
- **Deployment.** Workers and the gateway stay on 0.9.0: neither runs the index.

## 0.9.0 - 2026-10-03

The production chunk store's release (docs/rollout-0.8.0.md, "0.9.0"). It changes no image path. Every sandbox still mounts its EROFS root until a wave is switched, and `dispatch_roots` stays off.

- **nydusd in the sandbox bundle.** It is built by `runtime/nydusd/build_pinned.sh`: v2.4.5 `e3190057`, `block-nbd`, Rust 1.94.0, Apache-2.0. VM init verifies and installs it at `/usr/local/libexec/ucloud-sandboxes/nydusd`. `chunk_store.nydusd` naming that path is checked against the bundle's pin.
- **Stored locators and blob layouts.** Registration writes each component's locator and each nydusd blob's layout to the store. Workers and the store node then read them, not the index. M1 gate run 3 stalled for 4.5 minutes while one index process computed locators per attach.
- **nydusd's shared cache stays within `immutable_environments.cache_bytes`.** Idle images are detached, least recently used first.
- **M2 waves (C2.13):** `chunk-migrate convert` converts a wave's images with full-tree verification, one process per image. Rerunning it resumes. `record`, `switch`, `revert` and `status` run on the gateway, and a switch re-points the image's durable owners to the new closure. Retention keeps a reverted root. Release is not built yet (chunk-store-m2-plan.md §5.1).

## Earlier releases

Entries for 0.4.0 through 0.8.8 are in the git history:
`git show a04c1ad:CHANGELOG.md`.
