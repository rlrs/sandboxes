# ucloud-sandboxes

A self-hosted service for running AI agents and their tools in isolated Linux
sandboxes. Give each coding agent, evaluation task, or reinforcement-learning
rollout its own environment: choose an image, allocate CPU, memory and disk, run
commands, and read or write files through a Python SDK or HTTP API.

The service manages a shared pool of compute nodes. Production runs on **SDU
UCloud**; a Hetzner Cloud provider is also built in. Clients use one gateway URL and
API token; the backend handles placement, routing, image builds, and worker
scaling.

> **This repository is the server.** Clients never install it. To use sandboxes
> from verifiers, install
> [`verifiers-ucloud`](https://github.com/rlrs/verifiers-ucloud) at a tag
> (`uv add "verifiers-ucloud @ git+https://github.com/rlrs/verifiers-ucloud@v0.3.8"`),
> which pins the [SDK](https://github.com/rlrs/ucloud-sandboxes-sdk) release it
> was tested with. `main` is what production runs; releases are tagged `vX.Y.Z`
> and described in the [changelog](CHANGELOG.md).

A central feature is **sharing worker capacity across agents waiting for models**.
Managed agents stay resident when memory is available, so they can continue
quickly without checkpoint I/O. Under memory pressure, the service checkpoints
eligible agents' processes and filesystems, releases their active memory, and
restores them when needed. This combines fast ordinary turns with higher density
for workloads containing long model waits.

## What you can do

- **Run isolated tools and agents.** Execute commands, stream command output,
  transfer files, and keep a workspace across tool calls. Use synchronous or
  asynchronous Python clients, or call the HTTP API directly.
- **Bring your own environment.** Start from an OCI/container image, a prepared
  task image from the image index, or a recipe the service builds on demand.
  Images are mounted read-only on workers, which fetch what a sandbox reads
  (plus prefetch) instead of unpacking the whole image; training images live in
  a content-addressed chunk store.
- **Pause and resume work.** Park eligible sandboxes with process and filesystem
  state intact. Published checkpoints also support moving work to another
  compatible node and releasing idle workers.
- **Connect agents to your models.** An OpenAI-compatible model relay connects
  sandboxes to model endpoints, including workers reachable only through outbound
  connections. It coordinates parking and waking for managed agents.
- **Share and scale a compute pool.** Request resources per sandbox, prepare
  capacity ahead of a burst, and let the autoscaler grow or shrink sandbox and
  builder pools according to demand and configured policy.
- **Control access and observe workloads.** gVisor isolates sandbox processes;
  authenticated APIs and configurable network policies control access. Metrics,
  traces, and a dashboard expose placement, lifecycle, and resource usage.

Typical uses include coding-agent evaluations, parallel benchmark tasks, and
agentic RL rollouts. The Python SDK also includes an Inspect AI integration; other
runners can use the same sandbox API.

## How it works

```mermaid
flowchart LR
    Client["Your agent runner / SDK"] --> Gateway["Gateway<br/>(PostgreSQL)"]
    Gateway --> Sandboxes["Sandbox workers<br/>(ucloud-noded + gVisor)"]
    Gateway --> Builders["Image builders"]
    Builders --> Store["Store node<br/>(chunk store)"]
    Store --> Sandboxes
    Autoscaler["Autoscaler"] --> Provider["UCloud (or Hetzner)"]
    Provider --> Sandboxes
    Provider --> Builders
    Sandboxes --> Relay["Model relay"]
    Relay --> Models["Your model endpoint"]
```

The gateway places a sandbox on a suitable worker and routes subsequent requests
to it by sandbox ID. On each worker, the `ucloud-noded` daemon and the node agent
run gVisor sandboxes with the requested images and resources. Builders turn
recipes into images and publish them to the registry or the store node's chunk
store, from which workers mount them on demand. The autoscaler provisions provider VMs when the pool
needs more capacity. Your model inference runs separately; the relay connects it
to the sandbox workload. See [Architecture](docs/architecture.md) for the full
picture.

For a managed agent, the model-wait cycle is:

1. The agent runs tools inside its sandbox and sends a model request through the
   relay.
2. Once the relay has durably accepted the request, the sandbox waits in memory
   or parks to make room for other work. Inference continues independently.
3. The model worker commits its response to the relay. Durable acceptance releases
   the inference slot; delivery and any necessary wake continue independently.
   The agent receives the response with its existing process and workspace state.

This lifecycle requires a parkable, managed-process sandbox started with
`start_agent()` and registered with `register_agent_rollout()`. Setting
`parkable=True` alone does not wire up model-wait parking. Ordinary parkable
sandboxes can also wake on command or file requests. See the
[managed-agent and relay guide](docs/model-relay.md) for the complete setup.

Parking normally keeps the checkpoint on the same worker for a fast wake.
Publishing it to configured snapshot storage enables recovery or migration to
another compatible worker. Unpublished local state can be lost if its worker
fails; parking alone is not a durability guarantee.

## Use a sandbox

You need a running deployment and its **sandbox API token**. Install the
[Python SDK](https://github.com/rlrs/ucloud-sandboxes-sdk) using its installation
instructions, then configure the client:

```bash
export UCLOUD_SANDBOX_URL="https://sandbox-gateway.example.org"
export UCLOUD_SANDBOX_API_TOKEN="<your sandbox API token>"
```

This example creates a Python environment, runs a command, and cleans up:

```python
from uuid import uuid4

from ucloud_sandboxes_sdk import Image, SandboxClient, SandboxSpec

client = SandboxClient.from_env()
sandbox = client.create_sandbox(
    SandboxSpec(
        id=f"example-{uuid4().hex}",
        image=Image.from_registry("python:3.12-slim"),
        command=["sleep", "300"],
        cpus=1,
        memory_mb=2048,
        disk_mb=10240,
        ttl_seconds=600,
    )
)
try:
    result = sandbox.exec(
        ["python", "-c", "print('Hello from the sandbox')"],
        timeout_seconds=30,
    )
    if not result.success:
        raise RuntimeError(result.stderr)
    print(result.stdout)
finally:
    sandbox.delete()
```

The SDK repository covers file operations, asynchronous clients, image builds,
managed agents, and Inspect AI. For clients in other languages, use the
[HTTP API reference](docs/api-reference.md).

## Deploy and operate the service

This repository contains the server, runtime, autoscaler, and deployment tooling.
A deployment combines a persistent control plane (gateway with PostgreSQL, relay,
store node, and autoscaler) with sandbox and builder worker pools. Worker pools
can scale down when idle; the control plane stays available.

Start with [Deployment](docs/deployment.md); production operations are in the
[runbook](docs/operations-runbook.md). Inspect the configuration with:

```bash
uv run ucloud-sandboxes sample-config
```

Deployment provisions the required credentials and pinned runtime artifacts;
installing the Python client alone does not start a sandbox service.

Sandboxes provide CPU-based Linux userspace under gVisor, rather than a full guest
kernel per sandbox. Workloads that require systemd, reboot, kernel modules, or
host devices need separate compatibility consideration. Capacity and startup
latency depend on available workers, image preparation, and checkpoint storage;
preparing capacity helps avoid cold provisioning during a burst.

## Documentation

| Guide | Covers |
| --- | --- |
| [Architecture](docs/architecture.md) | Components, request paths, placement and routing |
| [State protocol](docs/state-protocol.md) | Durable state, ownership, and node-failure semantics |
| [API reference](docs/api-reference.md) | HTTP endpoints, fleet status, asynchronous responses, SDK |
| [Images](docs/images.md) | Image index, recipes, builds, toolkits, chunk store, registry |
| [Node admission and memory](docs/node-admission-and-memory.md) | What fits on a worker; resident, paused and parked sandboxes |
| [Model relay](docs/model-relay.md) | Managed agents, model waits, parking and waking |
| [Scaling policy](docs/scaling-policy.md) | Pools, autoscaling, prewarming, placement scoring |
| [Linux environments](docs/linux-environments.md) | Linux profiles and workload compatibility |
| [Security](docs/security.md) | Isolation, authentication, network policies |
| [Deployment](docs/deployment.md) | Machines, configuration, release bundles, providers, CLI |
| [Operations runbook](docs/operations-runbook.md) | Production procedures, backups, telemetry |
| [Roadmap](docs/roadmap.md) | Work still open |
| [Decisions](docs/decisions.md) | Why the system is built the way it is |

## Development

See [AGENTS.md](AGENTS.md) for the repository layout, how to run the checks, and
the rules for what goes into this public repository. In short:

```bash
bash scripts/check.sh
```

runs ruff, the Python suite, the Go and Rust runtime tests, and the SDK checkout's
suite.
