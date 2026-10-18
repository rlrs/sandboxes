import hashlib
import json
import sqlite3
import sys
import tarfile
import unittest
from collections.abc import Callable
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event


from sandboxes.images import (
    COMMAND_OUTPUT_TAIL_CHARS,
    COMMAND_OUTPUT_TRUNCATION_MARKER,
    DockerImageRuntime,
    ImageBuildConflictError,
    ImageBuildRecord,
    ImageBuildSpec,
    ImageBuildStore,
    ImageManager,
    ImageStore,
    MaterializedBuildContext,
    _extract_safe_tar_gz_file,
    image_build_fingerprint,
)
from sandboxes.sandbox import CommandResult

TEST_TIER = "contract"


class ImageTests(unittest.TestCase):
    def test_image_state_rejects_noncanonical_schemas_and_payloads(self) -> None:
        with TemporaryDirectory() as raw_dir:
            legacy_json = Path(raw_dir) / "legacy.json"
            legacy_json.write_text('{"images": []}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "invalid or unavailable"):
                ImageStore(legacy_json)

            for name, initialize, mutation in (
                (
                    "legacy-table",
                    False,
                    "CREATE TABLE images (payload TEXT) STRICT",
                ),
                (
                    "unfingerprinted",
                    False,
                    "CREATE TABLE image_state_v1_images "
                    "(record_id TEXT PRIMARY KEY, record_json TEXT NOT NULL) STRICT;"
                    "CREATE TABLE image_state_v1_builds "
                    "(record_id TEXT PRIMARY KEY, record_json TEXT NOT NULL) STRICT;",
                ),
                ("application-id", True, "PRAGMA application_id = 7"),
                ("version", True, "PRAGMA user_version = 2"),
                (
                    "columns",
                    True,
                    "DROP TABLE image_state_v1_images;"
                    "CREATE TABLE image_state_v1_images "
                    "(record_id TEXT PRIMARY KEY, payload TEXT NOT NULL) STRICT;",
                ),
            ):
                with self.subTest(name=name):
                    path = Path(raw_dir) / f"{name}.sqlite3"
                    if initialize:
                        ImageStore(path)
                    with sqlite3.connect(path) as conn:
                        conn.executescript(mutation)
                    with self.assertRaisesRegex(ValueError, "invalid or unavailable"):
                        ImageStore(path)

            path = Path(raw_dir) / "payload.sqlite3"
            store = ImageStore(path)
            with sqlite3.connect(path) as conn:
                conn.execute(
                    "INSERT INTO image_state_v1_images VALUES (?, ?)",
                    ("image-1", "{}"),
                )
            with self.assertRaises(ValueError):
                store.load()
            build_payload = _build_record(
                "build-1",
                status="running",
                timestamp="2026-01-01T00:00:00+00:00",
            ).to_dict()
            build_payload["command"] = "docker build"
            with sqlite3.connect(path) as conn:
                conn.execute(
                    "INSERT INTO image_state_v1_builds VALUES (?, ?)",
                    ("build-1", json.dumps(build_payload)),
                )
            with self.assertRaises(ValueError):
                ImageBuildStore(path).load()

    def test_streaming_runtime_retains_only_bounded_output_tail(self) -> None:
        runtime = DockerImageRuntime()
        delivered: list[str] = []
        output_size = COMMAND_OUTPUT_TAIL_CHARS * 3

        result = runtime._run_streaming(  # noqa: SLF001 - focused runtime regression
            (
                sys.executable,
                "-c",
                f"import sys; sys.stdout.write('a' * {output_size})",
            ),
            on_output=lambda _stream, chunk: delivered.append(chunk),
        )

        self.assertEqual("".join(delivered), "a" * output_size)
        self.assertEqual(len(result.stdout), COMMAND_OUTPUT_TAIL_CHARS)
        self.assertTrue(result.stdout.startswith(COMMAND_OUTPUT_TRUNCATION_MARKER))
        self.assertTrue(result.stdout.endswith("a" * 1024))

    def test_manager_marks_interrupted_build_failed_on_startup(self) -> None:
        with TemporaryDirectory() as raw_dir:
            image_path = Path(raw_dir) / "images.json"
            build_store = ImageBuildStore(Path(raw_dir) / "builds.json")
            build_store.upsert(
                ImageBuildRecord(
                    build_id="build-1",
                    image_id="base",
                    tag="local/base:latest",
                    status="running",
                    created_at="2026-01-01T00:00:00+00:00",
                    updated_at="2026-01-01T00:00:00+00:00",
                )
            )

            manager = ImageManager(
                ImageStore(image_path),
                DockerImageRuntime(dry_run=True),
                build_store=build_store,
            )

            build = manager.get_build("build-1")
            self.assertIsNotNone(build)
            assert build is not None
            self.assertEqual(build.status, "failed")
            self.assertIn("interrupted", build.error)
            self.assertTrue(build.finished_at)

    def test_build_singleflight_requires_exact_immutable_fingerprint(self) -> None:
        with TemporaryDirectory() as raw_dir:
            context_path = Path(raw_dir) / "context"
            context_path.mkdir()
            dockerfile = context_path / "Dockerfile"
            dockerfile.write_text("FROM scratch\n", encoding="utf-8")
            executor = BlockingBuildExecutor()
            build_store = ImageBuildStore(Path(raw_dir) / "builds.json")
            manager = ImageManager(
                ImageStore(Path(raw_dir) / "images.json"),
                DockerImageRuntime(executor=executor),
                build_store=build_store,
            )
            spec = ImageBuildSpec(
                id="singleflight",
                tag="local/singleflight:latest",
                context_path=str(context_path),
                build_args={"MODE": "one"},
            )
            context_identity, materialize = _uploaded_context(
                ("Dockerfile", b"FROM scratch\n")
            )

            first, started = manager.start_build(
                spec,
                context_identity=context_identity,
                materialize_context=materialize,
            )
            self.assertTrue(started)
            self.assertTrue(executor.started.wait(1))
            snapshot_path = Path(first.context_path)
            self.assertNotEqual(snapshot_path, context_path)
            self.assertEqual(
                (snapshot_path / "Dockerfile").read_text(encoding="utf-8"),
                "FROM scratch\n",
            )
            duplicate, duplicate_started = manager.start_build(
                spec,
                context_identity=context_identity,
                materialize_context=materialize,
            )
            self.assertFalse(duplicate_started)
            self.assertEqual(duplicate.build_id, first.build_id)
            self.assertEqual(
                build_store.load()[first.build_id].request_fingerprint,
                first.request_fingerprint,
            )

            cleanup_calls: list[str] = []
            differing = ImageBuildSpec(
                id=spec.id,
                tag=spec.tag,
                context_path=spec.context_path,
                build_args={"MODE": "two"},
            )
            with self.assertRaisesRegex(ImageBuildConflictError, "different"):
                manager.start_build(
                    differing,
                    context_identity=context_identity,
                    materialize_context=materialize,
                    cleanup=lambda: cleanup_calls.append("spec"),
                )
            with self.assertRaisesRegex(ImageBuildConflictError, "id or tag"):
                manager.start_build(
                    ImageBuildSpec(
                        id="other-id",
                        tag=spec.tag,
                        context_path=spec.context_path,
                    ),
                    context_identity=context_identity,
                    materialize_context=materialize,
                )
            old_fingerprint = image_build_fingerprint(
                spec,
                context_identity=context_identity,
            )
            dockerfile.write_text("FROM scratch\n# changed\n", encoding="utf-8")
            self.assertEqual(
                (snapshot_path / "Dockerfile").read_text(encoding="utf-8"),
                "FROM scratch\n",
            )
            changed_identity, changed_materialize = _uploaded_context(
                ("Dockerfile", b"FROM scratch\n# changed\n")
            )
            self.assertNotEqual(
                image_build_fingerprint(
                    spec,
                    context_identity=changed_identity,
                ),
                old_fingerprint,
            )
            with self.assertRaisesRegex(ImageBuildConflictError, "different"):
                manager.start_build(
                    spec,
                    context_identity=changed_identity,
                    materialize_context=changed_materialize,
                    cleanup=lambda: cleanup_calls.append("context"),
                )
            self.assertEqual(cleanup_calls, ["spec", "context"])

            executor.release.set()
            manager.wait_for_build(first.build_id, timeout_seconds=2)
            self.assertFalse(snapshot_path.exists())

    def test_manager_requires_uploaded_content_addressed_context(self) -> None:
        with TemporaryDirectory() as raw_dir:
            manager = ImageManager(
                ImageStore(Path(raw_dir) / "images.json"),
                DockerImageRuntime(dry_run=True),
            )
            _identity, materialize = _uploaded_context()

            with self.assertRaisesRegex(ValueError, "uploaded content-addressed"):
                manager.start_build(
                    ImageBuildSpec(
                        id="local-context",
                        tag="local/context:latest",
                        context_path=str(Path(raw_dir) / "context"),
                    ),
                    context_identity="tree:" + "a" * 64,
                    materialize_context=materialize,
                )

            self.assertEqual(manager.list_builds(), [])

    def test_context_archive_enforces_stream_and_member_limits(self) -> None:
        cases = (
            (
                "member",
                _tar_gz(("large", b"12345")),
                {"max_member_bytes": 4},
                "member exceeds",
            ),
            (
                "total",
                _tar_gz(("one", b"123"), ("two", b"456")),
                {"max_total_bytes": 5},
                "extracted-byte limit",
            ),
            (
                "count",
                _tar_gz(("one", b""), ("two", b"")),
                {"max_members": 1},
                "member limit",
            ),
            (
                "decompressed-pax",
                _tar_gz(("empty", b""), pax_value="x" * 4096),
                {"max_archive_bytes": 1024},
                "decompressed-byte limit",
            ),
        )
        for name, archive, limits, message in cases:
            with self.subTest(name=name), TemporaryDirectory() as raw_dir:
                with self.assertRaisesRegex(ValueError, message):
                    _extract_safe_tar_gz_file(
                        BytesIO(archive),
                        Path(raw_dir),
                        max_total_bytes=limits.get("max_total_bytes", 1024 * 1024),
                        max_member_bytes=limits.get("max_member_bytes", 1024 * 1024),
                        max_members=limits.get("max_members", 100),
                        max_archive_bytes=limits.get("max_archive_bytes", 1024 * 1024),
                    )

    def test_context_archive_rejects_absolute_links_and_special_files(self) -> None:
        with TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            destination = root / "destination"
            destination.mkdir()
            cases = (
                (str(root / "absolute"), tarfile.REGTYPE, ""),
                ("symlink", tarfile.SYMTYPE, "../escaped"),
                ("hardlink", tarfile.LNKTYPE, "../escaped"),
                ("character-device", tarfile.CHRTYPE, ""),
                ("block-device", tarfile.BLKTYPE, ""),
                ("fifo", tarfile.FIFOTYPE, ""),
            )
            for name, member_type, linkname in cases:
                with self.subTest(name=name), self.assertRaises(ValueError):
                    _extract_safe_tar_gz_file(
                        BytesIO(
                            _tar_gz_member(
                                name,
                                member_type=member_type,
                                linkname=linkname,
                            )
                        ),
                        destination,
                    )

            self.assertFalse((root / "absolute").exists())
            self.assertFalse((root / "escaped").exists())

    def test_dockerfile_must_be_normalized_context_relative_path(self) -> None:
        runtime = DockerImageRuntime(dry_run=True)
        for dockerfile in (
            "/tmp/Dockerfile",
            "../Dockerfile",
            "nested/../../Dockerfile",
            ".",
            "nested\\Dockerfile",
        ):
            with (
                self.subTest(dockerfile=dockerfile),
                self.assertRaisesRegex(
                    ValueError,
                    "dockerfile",
                ),
            ):
                runtime.build_command(
                    ImageBuildSpec(
                        id="base",
                        tag="local/base:latest",
                        context_path="/tmp/context",
                        dockerfile=dockerfile,
                    )
                )

        normalized = ImageBuildSpec.from_dict(
            {
                "id": "base",
                "tag": "local/base:latest",
                "context_path": "/tmp/context",
                "dockerfile": "nested/./Containerfile",
            }
        )
        self.assertEqual(normalized.dockerfile, "nested/Containerfile")

        with TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            context = root / "context"
            context.mkdir()
            outside = root / "outside.Dockerfile"
            outside.write_text("FROM scratch\n", encoding="utf-8")
            identity = "archive:sha256:" + "e" * 64

            def materialize_escaped_context() -> MaterializedBuildContext:
                temporary = TemporaryDirectory(prefix="test-image-context-")
                materialized_path = Path(temporary.name)
                (materialized_path / "Dockerfile").symlink_to(outside)
                return MaterializedBuildContext(
                    materialized_path,
                    temporary,
                    identity,
                )

            manager = ImageManager(
                ImageStore(root / "images.json"),
                DockerImageRuntime(dry_run=True),
            )
            with self.assertRaisesRegex(ValueError, "immutable build context"):
                manager.start_build(
                    ImageBuildSpec(
                        id="escaped-dockerfile",
                        tag="local/escaped-dockerfile:latest",
                        context_path=str(context),
                    ),
                    context_identity=identity,
                    materialize_context=materialize_escaped_context,
                )


def _uploaded_context(
    *entries: tuple[str, bytes],
) -> tuple[str, Callable[[], MaterializedBuildContext]]:
    archive = _tar_gz(*entries)
    identity = f"archive:sha256:{hashlib.sha256(archive).hexdigest()}"

    def materialize() -> MaterializedBuildContext:
        temporary = TemporaryDirectory(prefix="test-image-context-")
        context_path = Path(temporary.name)
        try:
            _extract_safe_tar_gz_file(BytesIO(archive), context_path)
        except Exception:
            temporary.cleanup()
            raise
        return MaterializedBuildContext(context_path, temporary, identity)

    return identity, materialize


class BlockingBuildExecutor:
    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()

    def run(
        self, argv: tuple[str, ...], *, input: bytes | None = None
    ) -> CommandResult:
        del input
        self.started.set()
        self.release.wait(2)
        return CommandResult(argv=argv, exit_code=0)


def _tar_gz(*entries: tuple[str, bytes], pax_value: str = "") -> bytes:
    payload = BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as archive:
        for name, content in entries:
            member = tarfile.TarInfo(name)
            member.size = len(content)
            if pax_value:
                member.pax_headers = {"comment": pax_value}
            archive.addfile(member, BytesIO(content))
    return payload.getvalue()


def _tar_gz_member(
    name: str,
    *,
    member_type: bytes,
    linkname: str = "",
) -> bytes:
    payload = BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as archive:
        member = tarfile.TarInfo(name)
        member.type = member_type
        member.linkname = linkname
        content = b"content" if member_type == tarfile.REGTYPE else b""
        member.size = len(content)
        archive.addfile(member, BytesIO(content) if content else None)
    return payload.getvalue()


def _build_record(
    build_id: str,
    *,
    status: str,
    timestamp: str,
) -> ImageBuildRecord:
    return ImageBuildRecord(
        build_id=build_id,
        image_id=f"image-{build_id}",
        tag=f"local/{build_id}:latest",
        status=status,
        created_at=timestamp,
        updated_at=timestamp,
        finished_at=timestamp if status in {"succeeded", "failed"} else "",
    )


if __name__ == "__main__":
    unittest.main()
