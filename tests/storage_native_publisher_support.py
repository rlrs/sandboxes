from __future__ import annotations

import hashlib
import json
from pathlib import Path
import socket

from sandboxes.storage_native import StorageNativeLayer


def digest(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


class FakeExporter:
    """Streams test layers through the publisher's real Unix-socket boundary."""

    def __init__(
        self,
        payloads: dict[Path, bytes],
        *,
        compact_payload: bytes = b"compact",
        wrong_digest: bool = False,
    ) -> None:
        self.payloads = payloads
        self.compact_payload = compact_payload
        self.wrong_digest = wrong_digest
        self.compact_calls: list[tuple[dict, Path]] = []

    def export_dense_layer(
        self,
        *,
        source_layer_path: Path,
        stream_socket_path: Path,
    ) -> StorageNativeLayer:
        return self._send(self.payloads[source_layer_path], stream_socket_path)

    def export_compacted_image(
        self,
        *,
        source_image_config: Path,
        global_config: Path,
        stream_socket_path: Path,
    ) -> StorageNativeLayer:
        self.compact_calls.append(
            (
                json.loads(source_image_config.read_text(encoding="ascii")),
                global_config,
            )
        )
        return self._send(self.compact_payload, stream_socket_path)

    def _send(self, payload: bytes, stream_socket_path: Path) -> StorageNativeLayer:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.connect(str(stream_socket_path))
            for offset in range(0, len(payload), 7):
                connection.sendall(payload[offset : offset + 7])
        observed = digest(payload)
        if self.wrong_digest:
            observed = "sha256:" + "0" * 64
        return StorageNativeLayer(digest=observed, size=len(payload))


class FakeRegistry:
    base_url = "http://registry:5000"

    def __init__(self) -> None:
        self.uploads: dict[str, bytearray] = {}
        self.blobs: dict[str, bytes] = {}
        self.manifests: dict[str, bytes] = {}
        self.next_upload = 0

    def start_blob_upload(self, _repository: str) -> str:
        self.next_upload += 1
        location = f"/v2/snapshots/blobs/uploads/{self.next_upload}"
        self.uploads[location] = bytearray()
        return location

    def upload_blob_chunk(self, location: str, chunk: bytes) -> str:
        self.uploads[location].extend(chunk)
        return location

    def abort_blob_upload(self, location: str) -> None:
        self.uploads.pop(location, None)

    def finish_blob_upload(self, location: str, digest: str) -> str:
        payload = bytes(self.uploads.pop(location))
        observed = f"sha256:{hashlib.sha256(payload).hexdigest()}"
        if observed != digest:
            raise ValueError("digest mismatch")
        self.blobs[digest] = payload
        return digest

    def blob_exists(self, _repository: str, digest: str) -> bool:
        return digest in self.blobs

    def put_manifest(
        self,
        _repository: str,
        reference: str,
        payload: bytes,
        *,
        media_type: str,
    ) -> str:
        digest = f"sha256:{hashlib.sha256(payload).hexdigest()}"
        self.manifests[reference] = payload
        self.manifests[digest] = payload
        return digest

    def manifest_document(
        self,
        _repository: str,
        reference: str,
    ) -> tuple[dict, dict[str, str]]:
        payload = self.manifests[reference]
        digest = f"sha256:{hashlib.sha256(payload).hexdigest()}"
        return (
            json.loads(payload.decode("ascii")),
            {"Docker-Content-Digest": digest},
        )

    def blob_bytes(
        self,
        _repository: str,
        digest: str,
        *,
        max_bytes: int,
    ) -> bytes:
        payload = self.blobs[digest]
        if len(payload) > max_bytes:
            raise ValueError("too large")
        return payload
