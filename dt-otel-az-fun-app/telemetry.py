"""OpenTelemetry setup: traces, metrics and logs to Dynatrace over OTLP/HTTP.

Reads DT_OTLP_ENDPOINT (e.g. https://<host>/e/<environment-id>/api/v2/otlp) and
DT_API_TOKEN (access token with openTelemetryTrace.ingest, metrics.ingest, logs.ingest).
"""

import functools
import logging
import os

import azure.functions as func
from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.propagate import extract
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import (
    Counter,
    Histogram,
    MeterProvider,
    ObservableCounter,
    UpDownCounter,
)
from opentelemetry.sdk.metrics.export import (
    AggregationTemporality,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

FLUSH_TIMEOUT_MILLIS = 2000

# Providers to flush at the end of each invocation.
_providers = []
_tracer = trace.get_tracer(__name__)


def _skip_exporter_logs(record: logging.LogRecord) -> bool:
    # Exporting the exporters' own warnings (e.g. "endpoint unreachable") would fail
    # and warn again, in an endless loop.
    return not record.name.startswith(("opentelemetry", "urllib3"))


def _resource() -> Resource:
    attributes = {
        "service.name": os.environ.get("OTEL_SERVICE_NAME", "dt-otel-az-fun-app"),
        "cloud.provider": "azure",
        "cloud.platform": "azure_functions",
    }
    # Set by the Azure Functions host; absent on local runs.
    if os.environ.get("WEBSITE_SITE_NAME"):
        attributes["faas.name"] = os.environ["WEBSITE_SITE_NAME"]
    if os.environ.get("REGION_NAME"):
        attributes["cloud.region"] = os.environ["REGION_NAME"]
    return Resource.create(attributes)


def configure_telemetry() -> None:
    """Initialize telemetry once per worker, before any handler runs."""
    try:
        endpoint = os.environ["DT_OTLP_ENDPOINT"].rstrip("/")
        headers = {"Authorization": f"Api-Token {os.environ['DT_API_TOKEN']}"}
    except KeyError:
        # Missing settings: keep serving requests, just without telemetry.
        logging.exception("Dynatrace telemetry is disabled")
        return

    resource = _resource()

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces", headers=headers)
        )
    )
    trace.set_tracer_provider(tracer_provider)

    # Dynatrace only accepts delta temporality; UpDownCounters stay cumulative.
    delta = AggregationTemporality.DELTA
    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=[
            PeriodicExportingMetricReader(
                OTLPMetricExporter(
                    endpoint=f"{endpoint}/v1/metrics",
                    headers=headers,
                    preferred_temporality={
                        Counter: delta,
                        ObservableCounter: delta,
                        Histogram: delta,
                        UpDownCounter: AggregationTemporality.CUMULATIVE,
                    },
                )
            )
        ],
    )
    metrics.set_meter_provider(meter_provider)

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(
            OTLPLogExporter(endpoint=f"{endpoint}/v1/logs", headers=headers)
        )
    )
    set_logger_provider(logger_provider)
    # Records logged inside a span carry its trace and span IDs.
    handler = LoggingHandler(level=logging.INFO, logger_provider=logger_provider)
    handler.addFilter(_skip_exporter_logs)
    logging.getLogger().addHandler(handler)

    _providers.extend([tracer_provider, meter_provider, logger_provider])

    # Outgoing calls made with `requests` become child spans and propagate the trace context.
    RequestsInstrumentor().instrument()


def traced_http(handler):
    """Wrap an HTTP-triggered function in a server span and flush telemetry afterwards.

    Continues the caller's trace when the request carries a `traceparent` header.
    The flush runs before the invocation returns, because the host may freeze the
    worker afterwards.
    """

    @functools.wraps(handler)
    def wrapper(req: func.HttpRequest, *args, **kwargs):
        try:
            with _tracer.start_as_current_span(
                handler.__name__,
                context=extract(dict(req.headers)),
                kind=trace.SpanKind.SERVER,
                attributes={
                    "faas.trigger": "http",
                    "http.request.method": req.method,
                    "url.full": req.url,
                },
            ) as span:
                response = handler(req, *args, **kwargs)
                span.set_attribute("http.response.status_code", response.status_code)
                if response.status_code >= 500:
                    span.set_status(trace.StatusCode.ERROR)
                return response
        finally:
            for provider in _providers:
                provider.force_flush(FLUSH_TIMEOUT_MILLIS)

    return wrapper
