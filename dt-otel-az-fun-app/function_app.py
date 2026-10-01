import logging
import time

import azure.functions as func
import requests
from opentelemetry import metrics, trace

from telemetry import configure_telemetry, traced_http

configure_telemetry()

tracer = trace.get_tracer(__name__)
meter = metrics.get_meter(__name__)
request_counter = meter.create_counter(
    "dt_otel_az_fun_app.requests", unit="1", description="Handled requests"
)
downstream_duration = meter.create_histogram(
    "dt_otel_az_fun_app.downstream.duration", unit="ms", description="Downstream call duration"
)

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)


# @traced_http goes directly above the function, below the @app.* decorators.
@app.function_name("hello")
@app.route(route="hello", methods=["GET"])
@traced_http
def hello(req: func.HttpRequest) -> func.HttpResponse:
    name = req.params.get("name", "world")

    with tracer.start_as_current_span("build-greeting") as span:
        span.set_attribute("greeting.name", name)
        greeting = f"Hello, {name}!"

    logging.info("Greeted %s", name)
    request_counter.add(1, {"function": "hello"})
    return func.HttpResponse(greeting, status_code=200)


@app.function_name("outbound")
@app.route(route="outbound", methods=["GET"])
@traced_http
def outbound(req: func.HttpRequest) -> func.HttpResponse:
    url = req.params.get("url", "https://httpbin.org/get")
    request_counter.add(1, {"function": "outbound"})

    with tracer.start_as_current_span("call-downstream") as span:
        span.set_attribute("downstream.url", url)
        started = time.monotonic()
        try:
            resp = requests.get(url, timeout=10)
        except requests.RequestException as exc:
            span.record_exception(exc)
            span.set_status(trace.Status(trace.StatusCode.ERROR, str(exc)))
            logging.error("Downstream call to %s failed: %s", url, exc)
            return func.HttpResponse(f"Downstream call failed: {exc}", status_code=502)
        finally:
            downstream_duration.record((time.monotonic() - started) * 1000)

    logging.info("Downstream %s returned %s", url, resp.status_code)
    return func.HttpResponse(
        f"Downstream {url} returned {resp.status_code}", status_code=200
    )
