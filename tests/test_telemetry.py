import time
import unittest
from threading import Event, Lock, current_thread

from opentelemetry import trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import Status, StatusCode

from sandboxes.telemetry import (
    OPERATION_DURATION_BUCKET_BOUNDARIES_SECONDS,
    NonBlockingBatchSpanProcessor,
    Telemetry,
    TelemetryHealth,
    TelemetrySettings,
)


class _BlockingExporter(SpanExporter):
    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()
        self.lock = Lock()
        self.export_threads: list[str] = []
        self.exported = 0

    def export(self, spans):
        with self.lock:
            self.export_threads.append(current_thread().name)
        self.started.set()
        self.release.wait(5)
        with self.lock:
            self.exported += len(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        self.release.set()


class TelemetryTests(unittest.TestCase):
    def test_operation_histogram_covers_hot_and_cold_paths(self) -> None:
        boundaries = OPERATION_DURATION_BUCKET_BOUNDARIES_SECONDS
        self.assertEqual(tuple(sorted(set(boundaries))), boundaries)
        self.assertLessEqual(boundaries[0], 0.001)
        self.assertIn(0.01, boundaries)
        self.assertIn(1.0, boundaries)
        self.assertGreaterEqual(boundaries[-1], 600.0)

    def test_settings_require_an_origin_and_bounded_queue(self) -> None:
        normalized = TelemetrySettings(
            endpoint="https://collector.example:4318/",
            max_queue_size=64,
            max_export_batch_size=32,
        ).validated()
        self.assertEqual(normalized.endpoint, "https://collector.example:4318")

        with self.assertRaisesRegex(ValueError, r"HTTP\(S\) origin"):
            TelemetrySettings(endpoint="https://collector.example/path").validated()
        with self.assertRaisesRegex(ValueError, "cannot exceed"):
            TelemetrySettings(
                max_queue_size=1,
                max_export_batch_size=2,
            ).validated()

    def test_disabled_telemetry_is_a_fast_noop(self) -> None:
        telemetry = Telemetry.disabled()
        with telemetry.span("test.noop"):
            pass

        self.assertFalse(telemetry.enabled)
        self.assertEqual(telemetry.health()["accepted_spans"], 0)

    def test_w3c_context_survives_mixed_case_transport_headers(self) -> None:
        first = Telemetry.create(
            TelemetrySettings(endpoint="", trace_sample_ratio=1.0),
            service_name="first",
            service_version="test",
            deployment_id="test",
        )
        # Use SDK providers directly here so the test does not open an OTLP
        # connection while still exercising the platform's carrier handling.
        first_provider = TracerProvider()
        second_provider = TracerProvider()
        first.tracer = first_provider.get_tracer("first")
        second = Telemetry.disabled("second")
        second.tracer = second_provider.get_tracer("second")

        with first.span("gateway") as root:
            headers: dict[str, str] = {}
            first.inject(headers)
            mixed_case = {"Traceparent": headers["traceparent"]}
            with second.span(
                "worker",
                parent_context=second.extracted_context(mixed_case),
            ) as child:
                self.assertEqual(child.trace_id, root.trace_id)

        first_provider.shutdown()
        second_provider.shutdown()

    def test_exporter_backpressure_never_blocks_request_threads(self) -> None:
        exporter = _BlockingExporter()
        health = TelemetryHealth(queue_capacity=4)
        processor = NonBlockingBatchSpanProcessor(
            exporter,
            health,
            max_queue_size=4,
            max_export_batch_size=1,
            export_interval_ms=60_000,
        )
        provider = TracerProvider()
        provider.add_span_processor(processor)
        tracer = provider.get_tracer("test")

        with tracer.start_as_current_span("occupy-exporter"):
            pass
        self.assertTrue(exporter.started.wait(1))
        caller_thread = current_thread().name
        started = time.perf_counter()
        for index in range(1_000):
            with tracer.start_as_current_span(f"span-{index}"):
                pass
        elapsed = time.perf_counter() - started
        snapshot = health.snapshot(queue_size=processor.queue_size, enabled=True)

        self.assertLess(elapsed, 1.0)
        self.assertGreater(snapshot["dropped_spans"], 0)
        self.assertNotIn(caller_thread, exporter.export_threads)
        exporter.release.set()
        provider.shutdown()

    def test_sampled_spans_record_thread_cpu_without_an_extra_metric(self) -> None:
        exporter = InMemorySpanExporter()
        health = TelemetryHealth(queue_capacity=8)
        processor = NonBlockingBatchSpanProcessor(
            exporter,
            health,
            max_queue_size=8,
            max_export_batch_size=1,
            export_interval_ms=60_000,
        )
        provider = TracerProvider()
        provider.add_span_processor(processor)
        telemetry = Telemetry.disabled("cpu-test")
        telemetry.tracer = provider.get_tracer("cpu-test")
        telemetry._tracer_provider = provider
        telemetry._processor = processor

        cpu_started = time.thread_time()
        with telemetry.span("cpu.work"):
            while time.thread_time() - cpu_started < 0.002:
                pass
        self.assertTrue(processor.force_flush())
        spans = exporter.get_finished_spans()

        self.assertEqual(len(spans), 1)
        attributes = spans[0].attributes
        self.assertGreater(attributes["ucloud.span.thread_cpu.duration"], 0)
        provider.shutdown()

    def test_turnaways_and_conflicts_are_counted_apart_from_errors(self) -> None:
        spans = InMemorySpanExporter()
        tracer_provider = TracerProvider()
        tracer_provider.add_span_processor(SimpleSpanProcessor(spans))
        reader = InMemoryMetricReader()
        meter_provider = MeterProvider(metric_readers=[reader])
        telemetry = Telemetry(
            settings=TelemetrySettings(),
            service_name="outcomes",
            tracer=tracer_provider.get_tracer("outcomes"),
            meter=meter_provider.get_meter("outcomes"),
            tracer_provider=tracer_provider,
            meter_provider=meter_provider,
            processor=None,
            health=TelemetryHealth(queue_capacity=1),
        )

        with telemetry.span("create.turned_away") as span:
            span.status = "deferred"
            # The HTTP layer marks every 5xx response afterwards.
            trace.get_current_span().set_status(Status(StatusCode.ERROR, "HTTP 503"))
        with self.assertRaises(LookupError):
            with telemetry.span("transaction.rolled_back") as span:
                span.status = "conflict"
                raise LookupError("serialization failure")
        with self.assertRaises(LookupError):
            with telemetry.span("transaction.failed"):
                raise LookupError("connection lost")
        with telemetry.span("create.refused") as span:
            span.status = "error"

        counted = {
            point.attributes["operation"]: point.attributes["status"]
            for metric in reader.get_metrics_data().resource_metrics[0].scope_metrics[0].metrics
            if metric.name == "ucloud.platform.operation.count"
            for point in metric.data.data_points
        }
        self.assertEqual(counted, {
            "create.turned_away": "deferred",
            "transaction.rolled_back": "conflict",
            "transaction.failed": "error",
            "create.refused": "error",
        })
        traced = {span.name: span.status.status_code for span in spans.get_finished_spans()}
        self.assertEqual(traced["create.turned_away"], StatusCode.OK)
        self.assertEqual(traced["transaction.rolled_back"], StatusCode.OK)
        self.assertEqual(traced["transaction.failed"], StatusCode.ERROR)
        tracer_provider.shutdown()
        meter_provider.shutdown()


if __name__ == "__main__":
    unittest.main()


class EventLoopLagTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocked_loop_records_late_wakeup(self):
        import asyncio
        from unittest.mock import Mock

        telemetry = Telemetry.disabled()
        telemetry.meter = Mock()
        record = telemetry.meter.create_histogram.return_value.record
        telemetry.observe_event_loop_lag(
            asyncio.get_running_loop(), "test-loop", interval_seconds=0.01,
        )
        await asyncio.sleep(0)  # Start the probe.
        await asyncio.sleep(0)
        time.sleep(0.1)  # Block the loop past the probe's deadline.
        for _ in range(100):
            if record.call_args_list:
                break
            await asyncio.sleep(0.01)
        lag, attributes = record.call_args_list[0].args
        self.assertGreaterEqual(lag, 0.05)
        self.assertEqual(attributes, {"loop": "test-loop"})
        self.assertEqual(
            telemetry.meter.create_histogram.call_args.args[0],
            "ucloud.platform.event_loop.lag",
        )
        with self.assertRaises(ValueError):
            telemetry.observe_event_loop_lag(
                asyncio.get_running_loop(), "bad", interval_seconds=0,
            )
        for task in asyncio.all_tasks():
            if task.get_name() == "event-loop-lag-test-loop":
                task.cancel()
