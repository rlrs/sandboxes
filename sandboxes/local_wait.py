"""Node-local model waits (pause-reclaim item 6; docs/architecture.md).

The node sees a sandbox's plaintext HTTP/1.1 calls to a private relay
endpoint (``sandbox.network_relays``: private IPv4 literals, or names for
private addresses, resolved when the node starts) at the TCP level. A call is
outstanding when the last relay payload went out; its answer is the next
inbound payload. The node pauses a sandbox whose call is outstanding and whose
cgroup has been idle, and thaws it on the answer's first packet, which a
paused gVisor network stack holds until then
(the 2026-10-03 node-local-wake run). Nothing reads payloads: nftables logs
only the IP and TCP headers of relay flows to one NFLOG group.

runtime/noded runs the waits (src/local_wait); the agent sends it the
endpoints and these constants (pause_handoff.pause_config).
"""
from __future__ import annotations

import ipaddress

TABLE = "ucloud_local_wait"
NFLOG_GROUP = 4207
SNAPLEN = 64  # IPv4 and the first 14 bytes of TCP, with IP options.
SETTLE_SECONDS = 0.05  # A call's request went out at least this long ago,
IDLE_SECONDS = 0.05  # and the sandbox used no more than IDLE_USEC of CPU in this window.
IDLE_USEC = 2000
# An answered call is watched this long, until the next request: any pause
# that lands on it is undone. A status read in progress re-pauses after it
# (keep_paused), and a thaw queued behind that read can lose to it.
ANSWERED_WATCH_SECONDS = 30.0


def relay_endpoints(relays, resolve=None):
    """(IPv4, port) of each relay at a private address: the plaintext path.

    A relay is a literal address, or a DNS name that ``resolve(name)`` answers
    with its current addresses (the network manager's ``relay_addresses``); a
    name counts when every answer is private. A name for public addresses is a
    TLS ingress: its waits keep the relay's park. Relay flows are matched after
    destination NAT, so a guest dialing a relay's virtual address is seen at
    the address it is translated to.
    """
    endpoints = set()
    for name, relay in relays.items():
        try:
            addresses = (ipaddress.IPv4Address(relay.host),)
        except ValueError:
            if resolve is None:
                continue
            try:
                addresses = tuple(ipaddress.IPv4Address(address) for address in resolve(name))
            except (OSError, ValueError):
                continue
        if addresses and all(address.is_private for address in addresses):
            endpoints.update((str(address), int(relay.port)) for address in addresses)
    return tuple(sorted(endpoints))
