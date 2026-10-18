# Security

This page describes what the system trusts, what it isolates, and how. It
covers the sandbox runtime, credentials, node bootstrap, and the network
boundary, including the configuration reference for sandbox network policies.

## Threat model

Sandboxes run arbitrary code: agent rollouts, benchmark tasks and whatever
those tasks download or generate. The system treats every sandbox workload as
hostile. A workload must not reach the host, other sandboxes, node-control or
storage services, or private infrastructure it was not explicitly granted.

Clients holding the sandbox API key are trusted to run workloads, not to
operate the fleet. The key can create and use sandboxes and images; it cannot
read operator telemetry, change placement, or reach worker APIs.

The following are trusted and form the trust boundary:

- the gateway host, its state, and the operators who hold the gateway control
  token;
- each worker host, including its privileged Warden, its firewall, and the
  pinned runtime it booted with;
- the private network between gateway, workers, store node and relay, together
  with host firewalling.

Out of scope:

- **Mutually untrusted node operators.** The node-control token is
  deployment-wide, so any worker holding it could call any other worker.
  Per-node identity would be the next hardening step if that enters scope.
- **Workloads that need a real kernel.** gVisor interposes a userspace kernel
  between the workload and the host kernel. It is a strong isolation layer, but
  it is not a VM. Workloads that depend on host kernel modules, nested
  container runtimes, privileged mounts, cgroups or exact `/proc` behaviour are
  outside the sandbox contract.
- **Secrets inside a sandbox.** Anything injected into a sandbox is visible to
  the workload.

## Sandbox runtime

Each worker runs one privileged Warden that invokes the deployment-pinned,
patched `runsc` binary directly. Docker and containerd are image
infrastructure only; they do not own sandbox processes, writable layers or
lifecycle state.

**Single lifecycle writer.** The Warden is the only writer of sandbox
lifecycle on a node. Every mutation carries the exact sandbox generation and a
stable operation id. The node rejects stale generations, conflicting retries,
and any mutation while admission is closed. Park, wake and delete are
generation-fenced and replay safely after a crash.

**Storage.** Root filesystems are OCI images materialised through the node
image cache. Writable volumes are storage-native devices with a hard physical
reservation; admission never relies on a soft writable-layer estimate. The
storage service, the Warden's registry and the gateway route must agree on
the same owner and generation before a sandbox is reported running. Migration
requires the storage-native schema and capability on both nodes, and manifest
and snapshot digests fence every ownership transfer.

**No host sockets.** Node-control and storage sockets are never mounted into a
workload.

### Workload policy

Every sandbox request carries explicit CPU, memory, disk, user, capability,
PID and network policy, and the node validates them before building the OCI
bundle. The default `container` profile runs as UID/GID 1000, drops all
capabilities, sets `no_new_privileges`, limits the sandbox to 256 PIDs, and
bounds its temporary filesystems. A request that deliberately grants root or
capabilities gets exactly that weaker policy and no more. Host-oriented
bootstrap behaviour needs the explicit `linux_host` profile. The profiles and
their defaults are in [Linux environments](linux-environments.md); the request
fields are in the [API reference](api-reference.md#create-a-sandbox).

Inject secrets at execution time and treat them as visible to the workload.
Never write them into images, node labels, route metadata, logs or snapshot
manifests.

## Credentials

A deployment generates four independent bearer tokens. Each is stored in its
own state file; an empty token file is a startup error.

| Credential | State file | Holder | Grants |
|---|---|---|---|
| Sandbox API key | `sandbox-api-token` | SDK clients | Least-privileged public access: create and operate sandboxes, exec, files, managed jobs, build contexts and image builds, image pulls and the image index, prepared capacity. |
| Gateway control token | `gateway-token` | Operators, autoscaler, model relay | Every gateway route, including nodes, demand, metrics, registry, and explicit park, wake, detach and migration. |
| Heartbeat token | `heartbeat-token` | Workers | `POST /v1/nodes/heartbeat` only. |
| Node-control token | `node-control-token` | Gateway | Every node agent route except `/healthz`. |

The model relay has its own credentials (`relay-sandbox-token`,
`relay-worker-token`) and per-registration capability URLs, so a harness never
needs a deployment-wide token; see [Model relay](model-relay.md).

How the boundaries hold:

- The gateway decides per route whether the sandbox API key may call it
  ([`gateway/auth.py`](../ucloud_sandboxes/gateway/auth.py)); everything else
  returns `403` for that key. The list is in the
  [API reference](api-reference.md#endpoint-index).
- Both public credentials are accepted as `Authorization: Bearer` or as
  `X-UCloud-Sandbox-Token`, because UCloud public links can consume the
  standard header. Never distribute the gateway control token as an SDK key.
- When the gateway forwards a request to a worker, it strips `Authorization`,
  `Proxy-Authorization` and `X-UCloud-Sandbox-Token` and attaches only the
  node-control token. A public credential never reaches a worker.
- Heartbeat and node-control tokens are accepted only as `Authorization:
  Bearer` and are never valid at public SDK routes.
- Public deployments terminate trusted TLS in front of the gateway, so no
  credential crosses the Internet in plaintext.
- `/healthz` reveals service identity, health and version only.
- Rotate each credential independently. Rotating the gateway token also
  invalidates outstanding signed exec session prefixes; those sessions then
  read as unknown.

## Bootstrap and supply chain

Nodes boot from one role-specific package bundle staged by the control plane.
The bundle's SHA-256 digest is mandatory. VM init verifies:

- the exact Ubuntu release, architecture, kernel release and node role;
- the digest of every Docker, gVisor, support package and kernel module;
- the preassembled Python agent runtime;
- on sandbox nodes, the pinned patched `runsc`, the managed PID 1 helper, and
  the storage-native backend.

Any missing, corrupt or mismatched artifact aborts bootstrap. Nodes never add
package repositories or download a substitute runtime while booting. A node
advertises readiness only after its exact bundle and services pass start-up
checks. See [Deployment](deployment.md).

Image environment roots are signed with an Ed25519 producer key
(`provision-environment-key`). Workers read roots only against a trusted key
file that must be owned by root or the service user and not writable by
others.

**Build-time network access.** Image builds reach package mirrors through the
store node's package cache, which only serves allowlisted hosts over `GET` and
`HEAD`. With a GitHub token configured, the same cache serves read-only git
fetches (`info/refs` and `git-upload-pack`) from `github.com`; it forwards no
pushes and no other paths or hosts, and the token never enters a build.

## Network isolation

Each sandbox request selects `network`: `bridge` (the default) or `none`.
Each worker runs one node-wide network mode, set by
`serve-direct-node-agent --network`:

| Mode | Meaning |
|---|---|
| `none` (CLI default) | No sandbox networking. Accepts only `network: "none"` sandboxes. |
| `sandbox` | Each sandbox gets a dedicated network namespace, a veth pair on `100.96.0.0/16`, and NAT. Accepts only `network: "bridge"` sandboxes. |

Deployed sandbox nodes run in `sandbox` mode. A request whose `network` does
not match the worker's mode is rejected by the worker.

In `sandbox` mode the host enforces, for every sandbox:

- **No host access.** Traffic from the sandbox network to the host's own
  INPUT path is dropped.
- **No private destinations.** Forwarding from the sandbox network to
  `10.0.0.0/8`, `100.64.0.0/10`, `127.0.0.0/8`, `169.254.0.0/16`,
  `172.16.0.0/12` and `192.168.0.0/16` is dropped, so sandboxes cannot reach
  workers, the gateway, the store node or cloud metadata.
- **IPv4 only.** Sandboxes get IPv4 addresses and NAT only. Pairs that
  `noded` builds have IPv6 disabled on both ends of the veth, and relay
  policies drop IPv6 outright.
- **Public DNS.** A direct-egress sandbox resolves through `1.1.1.1` and
  `8.8.8.8`, or through up to three `dns_servers` from its request. Public DNS
  does not know private names.

Every create rechecks the host firewall rules (one `iptables-save` and one
`sysctl`) unless a check started after the request arrived, and restores any
rule deleted since. Concurrent creates share one check.

Inbound SSH, when a sandbox enables it, binds to the worker's localhost. Reach
it through the gateway or a tunnel rather than exposing container ports.

### Private service exceptions

`sandbox.direct_network_allow_tcp` lists exact private TCP services, as
`IPV4:PORT` or `HOST:PORT`, that every direct-egress sandbox may reach. They
become exact rules above the private-destination drops; a hostname is resolved
on the worker into `/32` rules, and no resolver or general private access is
exposed to the sandbox. Prefer IPv4 literals here. Any private service that
sandboxes reach by name is better configured as a [relay](#network-policy):
the sandbox cannot resolve the private name itself, and the Rust create path
hands creates with DNS-named exceptions to the Python agent.

## Network policy

`network` selects connectivity; `network_policy` selects which egress is
permitted. They are separate so that a relay can later provide a filtering
proxy without changing the isolation mechanism.

| Policy | Meaning |
|---|---|
| `{"egress": "direct"}` (default) | The bridge and NAT behaviour above, with its private-destination drops and configured exceptions. The SDK omits this default. |
| `{"egress": "relay", "relay": "<name>"}` | Only the named relay's TCP endpoint is reachable. Requires `network: "bridge"`; rejects `dns_servers` and inbound SSH. |

The relay name refers to trusted worker configuration. Sandbox callers cannot
supply destination addresses, firewall rules or exceptions.

### Configuration

| Key or flag | Default | Meaning |
|---|---|---|
| `sandbox.network_relays` | `{}` | Map of relay name to `HOST:PORT`. Names match `[a-z][a-z0-9-]{0,31}`; at most 64 relays. |
| `sandbox.direct_network_allow_tcp` | `[]` | Exact private TCP services every direct-egress sandbox may reach; see above. |
| `serve-direct-node-agent --network-relays-json` | `{}` | The relay map for a standalone node. |
| `serve-direct-node-agent --direct-network-allow-tcp HOST:PORT` | none | Repeatable; the allow list for a standalone node. |

```json
{
  "sandbox": {
    "network_relays": {
      "default": "relay.example.org:443"
    }
  }
}
```

- Use the hostname and port that sandbox clients use, including the hostname
  the relay's TLS certificate requires.
- Name a private relay by its VM's DNS name (for example
  `relay-vm:8092`; 8092 is the model relay's port). Private addresses change
  when a VM restarts; the DNS name does not. An IPv4 literal also works for an
  endpoint whose address never changes.
- The relay must run on a different host from the worker; worker-local
  services stay blocked. Relays may not resolve to loopback, link-local,
  multicast, reserved or sandbox-network addresses.
- Use a dedicated endpoint. This is TCP isolation, not HTTP path, virtual-host
  or application authorisation.
- Use the same relay definitions across a worker pool. Names are stable policy
  identities: introduce a new name when the hostname or port changes. Changing
  the DNS answers for a hostname needs no policy change.

Workers need `nftables`, `iptables` and `iproute2`; the node package bundle
includes them. At start-up a worker checks nftables support and advertises
`network-policy-relay-v1:<name>` for each configured relay. The gateway
requires that capability during placement and migration, so a worker without
it can never accept a restricted sandbox as an unrestricted one. A relay no
worker advertises has no eligible worker; a node asked directly for an
unconfigured relay rejects it. Migration preserves the policy in the sandbox
manifest.

### Relay addresses inside a sandbox

A relay named by DNS gets a stable **virtual address** in `198.18.0.0/15`,
derived from its name (`NetworkRelay.virtual_ip` in
[`relay_network.py`](../ucloud_sandboxes/relay_network.py)). Every sandbox
reaches a relay by its hostname, whatever its egress, and the worker
translates the virtual address to the name's current address, re-resolved on
the host every 2 seconds. Sandboxes never resolve private names themselves.

- A **relay-only** sandbox's `/etc/hosts` maps its relay's hostname to the
  virtual address. It has no DNS.
- A **direct-egress** sandbox keeps public DNS, and its `/etc/hosts` maps every
  configured relay's hostname to its virtual address. The worker's shared
  `bridge_nat` chain translates the address, and `bridge_forward` lets exactly
  those flows past the private-destination drops.

Point guests at `http://<relay hostname>:<port>` (in verifiers-ucloud,
`guest_relay_url`). The URL survives the relay host's address changes. A relay
given as an IPv4 literal is its own virtual address and needs no translation.

Clients inside the sandbox must honour `/etc/hosts`; clients that make their
own DNS queries fail closed. Editing guest files or dialling a numeric address
grants no other destination.

### From the SDK

```python
from ucloud_sandboxes_sdk import (
    Image, SandboxClient, SandboxNetworkPolicy, SandboxSpec, model_relay_env,
)

client = SandboxClient.from_env()
sandbox = client.create_sandbox(SandboxSpec(
    id="restricted-rollout",
    image=Image.from_registry("your-prepared-agent-image"),
    command=["python", "/app/agent.py"],
    memory_mb=1024,
    cpus=1,
    disk_mb=2048,
    network="bridge",
    network_policy=SandboxNetworkPolicy.relay_only("default"),
    env=model_relay_env(
        "https://relay.example.org", "run-001",
        api_key="<sandbox-scoped-relay-token>",
    ),
))
```

The async client and `SandboxSpec.benchmark()` accept the same policy. The
Inspect AI provider selects it with `UCLOUD_SANDBOX_RELAY=<name>`. Install
dependencies in the image before starting a restricted workload; package
downloads from inside it are blocked.

### Enforcement

Relay policies live in one shared nftables table, `inet ucloud_relay`. Its
base chains dispatch on the sandbox's host-side veth through verdict maps to
that sandbox's own chains, so each packet costs a constant number of lookups
however many restricted sandboxes a node hosts. Rules match the host-side
interface and the expected source address, not the sandbox's claimed source
address alone.

- The pre-DNAT guard permits only the selected relay route and drops
  everything else: IPv6, UDP and DNS, other ports, direct Internet access and
  source spoofing. Its drop is final even if Docker or older rules later
  accept.
- The host INPUT and OUTPUT paths for restricted sandboxes are dropped.
- The forward guard accepts only the configured relay endpoints and
  established TCP replies from them. Packets it authorises are marked, and one
  scoped iptables rule lets marked packets past the older private-address
  drops and Docker's FORWARD policy.
- For a DNS-named relay, host-side DNAT translates the virtual address to a
  resolved address, preserving the client's hostname and TLS SNI. No resolver
  is exposed to the guest.

**DNS changes.** The worker resolves each relay on the host every 2 seconds and
atomically replaces the affected rules when the answer changes. New
connections use a deterministic address from the current A records; existing
connections continue while their address stays authorised. Removing an
address revokes its flows, so clients must reconnect. An empty, invalid or
failed answer installs a deny-all policy until a valid answer returns. IPv6
relay upstreams are not supported.

**Lifecycle.** Policy intent is recorded durably before the veth is handed to
a sandbox. A failed firewall transaction prevents the handoff and leaves the
previous rules in place. Create, restore and migration all install the policy
before the sandbox runs. A node agent restart rebuilds rules from durable
leases before broad forwarding rules are reconciled. Deletion keeps the policy
and network slot reserved until the interface is gone, then removes the owned
rules before the slot can be reused.

Direct-egress creates are handled by the Rust `noded` daemon, which uses the
same slot state, names, addresses and iptables rules as the Python agent. A
create that needs a relay policy or a DNS-named egress endpoint is forwarded to
the Python agent.

**Pre-created pairs.** Each worker keeps a pool of 32 configured namespace and
veth pairs, so a burst of creates does not build them on the request path. A
pooled pair sits in an empty namespace with no process and carries no traffic.
Its slot moves from the pool to the lease in the same durable write that
records the lease; the policy is installed afterwards. A background thread
refills the pool and defers to in-flight creates for up to one second per
pair. When the pool is empty, a create builds its own pair.

Like all host-enforced isolation, this assumes the privileged host and its
firewall administration remain trusted.

### Filtering relays

A named endpoint can already be an application relay or a dedicated HTTP
CONNECT, SOCKS or filtering proxy: proxy-aware clients use it while the same
host policy prevents bypass. The model relay is not a general-purpose
Internet proxy, and relay egress does not intercept arbitrary HTTP or TLS. Do
not expose an unrestricted forwarding endpoint when the intended policy is
limited model access. Per-sandbox filtering, destination allow lists or
auditing belong at that relay, and should arrive as a new versioned policy and
worker capability rather than a reinterpretation of `relay`.

### Testing

Unit and protocol tests cover serialisation, validation, placement
requirements, create and migration propagation, atomic update failure, DNS
handoff and failure, restart, name-file safety and slot clean-up. The Linux
packet test runs in a disposable network and mount namespace with private
fixture services and exercises the real host firewall and namespace wiring:

```sh
sudo env UCLOUD_RUN_NETNS_TESTS=1 PYTHONPATH="$PWD" \
  "$(command -v python3)" tests/test_relay_network_linux.py
```

It needs `unshare`, `mount`, `ip`, `iptables` and `nft`, and CI runs it on
Linux. It does not replace a deployment smoke test with the pinned gVisor
runtime and relay TLS. `tests/test_direct_network_pool.py` hands a pooled pair
to a lease with real `ip`, namespaces and bind mounts, without a firewall; it
runs without root wherever unprivileged user namespaces are enabled.

## Operational invariants

- A node advertises readiness only after its exact bundle and services pass
  start-up checks.
- Disk capacity is credited only from storage-native physical authority.
- Park, wake and delete are generation-fenced and crash-replayable.
- A draining node closes admission before it reports drain progress.
- `/healthz` reveals service health and version only.
- The four deployment credentials rotate independently.
