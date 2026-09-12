"""OpenTelemetry tracing (D5): one trace per request, stage spans, GenAI-convention attributes on LLM calls.

Default exporter appends JSON spans to .cache/traces.jsonl (no collector needed). If
OTEL_EXPORTER_OTLP_ENDPOINT is set, spans are also exported over OTLP/HTTP. Falls back to a
no-op tracer if the SDK is missing so the run never blocks.
"""
from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

try:
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
    _HAVE_OTEL = True
except Exception:  # pragma: no cover
    _HAVE_OTEL = False


class _Span:
    def __init__(self, otel_span=None):
        self._s = otel_span

    def set(self, **attrs):
        if self._s is not None:
            for k, v in attrs.items():
                if v is not None:
                    self._s.set_attribute(k, v if isinstance(v, (str, int, float, bool)) else str(v))

    def set_genai(self, system: str, usage: dict, fallback_used: bool):
        self.set(**{"gen_ai.system": system, "gen_ai.request.model": usage.get("model"),
                    "gen_ai.usage.input_tokens": usage.get("input_tokens"), "gen_ai.usage.output_tokens": usage.get("output_tokens"),
                    "gen_ai.response.finish_reasons": usage.get("finish_reason"), "buyorwait.fallback_used": fallback_used,
                    "buyorwait.error": usage.get("error")})


class _NoopTracer:
    @contextmanager
    def span(self, name, **attrs):
        yield _Span(None)

    def flush(self):
        pass


if _HAVE_OTEL:
    class JsonFileSpanExporter(SpanExporter):
        def __init__(self, path: Path):
            self.path = path
            path.parent.mkdir(parents=True, exist_ok=True)
            self._f = open(path, "a", encoding="utf-8")

        def export(self, spans):
            for s in spans:
                ctx = s.get_span_context()
                rec = {"name": s.name, "trace_id": format(ctx.trace_id, "032x"), "span_id": format(ctx.span_id, "016x"),
                       "parent_id": format(s.parent.span_id, "016x") if s.parent else None,
                       "start": s.start_time, "end": s.end_time, "duration_ms": (s.end_time - s.start_time) / 1e6,
                       "attributes": dict(s.attributes or {}), "status": str(s.status.status_code)}
                self._f.write(json.dumps(rec, default=str) + "\n")
            self._f.flush()
            return SpanExportResult.SUCCESS

        def shutdown(self):
            self._f.close()

    class _OtelTracer:
        def __init__(self, path: Path):
            provider = TracerProvider(resource=Resource.create({"service.name": "buyorwait", "service.version": "1.0"}))
            self._exporter = JsonFileSpanExporter(path)
            provider.add_span_processor(SimpleSpanProcessor(self._exporter))
            endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
            if endpoint:
                try:
                    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
                    from opentelemetry.sdk.trace.export import BatchSpanProcessor
                    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
                except Exception as e:  # pragma: no cover
                    print(f"OTLP exporter disabled: {e}")
            self._provider = provider
            self._tracer = provider.get_tracer("buyorwait")

        @contextmanager
        def span(self, name, **attrs):
            with self._tracer.start_as_current_span(name) as s:
                sp = _Span(s)
                sp.set(**attrs)
                yield sp

        def flush(self):
            self._provider.force_flush()


_TRACER = None


def get_tracer(path: Path):
    global _TRACER
    if _TRACER is None:
        if _HAVE_OTEL and os.environ.get("BUYORWAIT_TRACING", "1") != "0":
            # start a fresh trace file per run
            if path.exists():
                path.unlink()
            _TRACER = _OtelTracer(path)
        else:
            _TRACER = _NoopTracer()
    return _TRACER
