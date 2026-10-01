# Azure Function App (Python) with OpenTelemetry to Dynatrace

A small Python Azure Function App (v2 programming model) that sends **traces, metrics and logs**
to Dynatrace with standard OpenTelemetry (OTLP/HTTP). No Dynatrace-specific packages are used.

| Endpoint | What it does | Telemetry |
| --- | --- | --- |
| `GET /api/hello?name=x` | Returns `Hello, x!` | Server span + `build-greeting` child span, request counter, log line |
| `GET /api/outbound?url=...` | Calls `url` (default `https://httpbin.org/get`) | Server span + `call-downstream` span + outgoing HTTP span, duration metric, log line |

Both endpoints are anonymous (no function key), so keep the app stopped when you are not testing.

## Defaults

| | |
| --- | --- |
| Function app | `dt-otel-az-fun-app` |
| Resource group | `rg-dt-otel-test` (swedencentral) |
| Plan | Linux Consumption, Python 3.12 |
| Default state | **Stopped**. Start it only for tests (see below). |

All of these are set in the vault file (see [Configuration](#configuration-the-vault-file)).

## How telemetry is sent

`telemetry.py` sets up the OpenTelemetry SDK once per worker and exports to
`<DT_OTLP_ENDPOINT>/v1/traces`, `/v1/metrics` and `/v1/logs`, authenticated with `DT_API_TOKEN`.

- **Traces:** `@traced_http` wraps each function in a server span. If the caller sends a
  `traceparent` header, the span joins the caller's trace. Outgoing calls made with `requests`
  are traced automatically and pass the trace context on.
- **Metrics:** sent with delta temporality, which Dynatrace requires.
- **Logs:** Python `logging` records at INFO or above are exported, linked to the active trace.
- **Flush:** telemetry is exported before each invocation returns, because Azure may freeze the
  worker afterwards.

## What is sent, and what is not

This app sends **the telemetry its own Python code produces**. It does not send everything the
Azure Functions platform knows about the app.

| Telemetry | Sent by this app? | How to get it into Dynatrace |
| --- | --- | --- |
| Spans for each function invocation, custom spans, outgoing `requests` calls | Yes | — |
| Custom metrics created with the OpenTelemetry meter | Yes | — |
| Python log lines (INFO and above) | Yes | — |
| Calls made with other HTTP clients (`httpx`, `aiohttp`, `urllib`) or SDKs (databases, Azure SDK) | No | Add the matching `opentelemetry-instrumentation-*` package and call its `instrument()` in `telemetry.py` |
| Platform metrics: executions, execution time, memory, HTTP 5xx, etc. | No | Dynatrace **Azure Monitor integration** (configured in Dynatrace, no code change) |
| Functions host logs (function started/completed, host errors) and Azure activity/diagnostic logs | No (they go to Application Insights) | Dynatrace **Azure log forwarding** |
| Functions host traces | No | The Functions host has an OpenTelemetry mode (`"telemetryMode": "OpenTelemetry"` in `host.json` plus `OTEL_EXPORTER_OTLP_*` app settings) that could send them to the same endpoint. Not set up or tested here. |

In short: application-level traces, metrics and logs come from this code; infrastructure-level
data comes from the Dynatrace Azure integrations.

## Prerequisites

- Azure CLI, logged in: `az login` (check the active subscription with `az account show`)
- Azure Functions Core Tools (`func`)
- Python 3.12 or later on your machine (only needed for local runs)
- Permission to create resources in the target subscription and resource group
- A Dynatrace access token (see next section)

## Configuration: the vault file

Settings and the token are kept in `vault/vault.cfg`, which is git-ignored and never committed.
Create it from the example:

```bash
cp vault/vault.cfg.example vault/vault.cfg
# then edit vault/vault.cfg and fill in the values
```

To use a vault file somewhere else, set `VAULT_FILE=/path/to/vault.cfg` when running the scripts.
`deploy.sh` and `run_local.sh` load it. Format: one `KEY=value` per line, **no spaces around
`=`**, no quotes needed.

| Variable | What it is |
| --- | --- |
| `APP_NAME` | Function app name; must be globally unique in Azure. Also used as the service name in Dynatrace. |
| `LOCATION` | Azure region, e.g. `swedencentral` |
| `RESOURCE_GROUP` | Resource group; created if it does not exist |
| `DT_API_TOKEN` | Dynatrace access token (`dt0c01.`). Create it under **Access tokens > Generate new token** with the scopes **Ingest OpenTelemetry traces** (`openTelemetryTrace.ingest`), **Ingest metrics** (`metrics.ingest`) and **Ingest logs** (`logs.ingest`). |
| `DT_OTLP_ENDPOINT` | `https://<managed-host>/e/<environment-id>/api/v2/otlp` (your Dynatrace environment URL plus `/api/v2/otlp`) |

Values in the vault file take precedence over the same variables set on the command line.

## Deploy

```bash
cd dt-otel-az-fun-app
./deploy.sh
```

The script:

1. loads the vault file and checks the required values are present;
2. creates the resource group, storage account and function app if they do not exist (an
   existing resource group is left untouched);
3. applies tags to the storage account and function app;
4. writes `DT_OTLP_ENDPOINT`, `DT_API_TOKEN` and `OTEL_SERVICE_NAME` (= `APP_NAME`) as app
   settings;
5. publishes the code (built remotely in Azure).

Re-run it after any code or vault change. Re-deploying a stopped app does **not** start it.

Further optional variables:

| Variable | Default |
| --- | --- |
| `PYTHON_VERSION` | `3.12` |
| `STORAGE_ACCOUNT` | derived from `APP_NAME` |
| `TAGS` | `Application=<APP_NAME> Purpose=dynatrace-traces DeployedBy=deploy.sh` |

## Run a test

Replace the app and resource group names if you changed them in the vault file.

```bash
# 1. start the app
az functionapp start -n dt-otel-az-fun-app -g rg-dt-otel-test

# 2. generate traffic (the first call after a start can take a few seconds)
for i in $(seq 1 10); do
  curl -s -w " [%{http_code}]\n" "https://dt-otel-az-fun-app.azurewebsites.net/api/hello?name=test$i"
  curl -s -w " [%{http_code}]\n" "https://dt-otel-az-fun-app.azurewebsites.net/api/outbound"
  sleep 1
done

# optional: one failing call, to see an error span and an error log
curl "https://dt-otel-az-fun-app.azurewebsites.net/api/outbound?url=https://does-not-exist.invalid"

# 3. stop the app again
az functionapp stop -n dt-otel-az-fun-app -g rg-dt-otel-test
```

Expected responses: `Hello, test1! [200]` and
`Downstream https://httpbin.org/get returned 200 [200]`. A stopped app answers `403`.

Check the state at any time with:

```bash
az functionapp show -n dt-otel-az-fun-app -g rg-dt-otel-test --query state -o tsv
```

## Verify in Dynatrace

Wait 1–2 minutes after sending traffic, set the timeframe to the last 30 minutes, and look for
the service named after `APP_NAME`:

| Signal | Where | What to look for |
| --- | --- | --- |
| Traces | **Distributed traces**, view **Ingested traces** | Requests `hello` and `outbound`; open one to see the child spans |
| Metrics | Metrics browser / Data Explorer | `dt_otel_az_fun_app.requests` (per `function`), `dt_otel_az_fun_app.downstream.duration` (ms) |
| Logs | Log viewer, filter on the service name | `Greeted test1`, `Downstream ... returned 200` (linked to their traces) |

## Run locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
./run_local.sh
```

Then call `http://localhost:7071/api/hello?name=local` and
`http://localhost:7071/api/outbound`. Stop with Ctrl+C.

Local runs use the same vault file and send real telemetry to Dynatrace. To keep it apart from
the Azure app, run with a different service name:

```bash
OTEL_SERVICE_NAME=dt-otel-az-fun-app-local ./run_local.sh
```

Exported log records include the source file path, which on your machine contains your
username.

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `deploy.sh` stops with `set ... in vault.cfg` | A required value is missing from the vault file. |
| `command not found: ...` when the scripts start | Spaces around `=` in the vault file. Use `KEY=value`. |
| App log shows `Dynatrace telemetry is disabled` | `DT_OTLP_ENDPOINT` or `DT_API_TOKEN` is missing from the app settings. Re-run `deploy.sh`. |
| App log shows `Failed to export ... 401` | `DT_API_TOKEN` is wrong, expired or missing a scope. |
| App log shows `Failed to export ... 404` or `302` | `DT_OTLP_ENDPOINT` is wrong; it must end in `/api/v2/otlp`. |
| Endpoints answer `403` | The app is stopped. Start it. |
| No data in Dynatrace, no errors | Wait a couple of minutes and widen the timeframe; check you are on **Ingested traces**. |

`func azure functionapp logstream` does not work on Linux Consumption. To see the app's logs,
use Application Insights (created by the deployment, same name as the app) in the Azure portal,
or:

```bash
func azure functionapp logstream dt-otel-az-fun-app --browser
```

## Project layout

| File | Purpose |
| --- | --- |
| `function_app.py` | The two HTTP functions and the example metrics |
| `telemetry.py` | OpenTelemetry setup (traces, metrics, logs) and the `@traced_http` decorator |
| `requirements.txt` | Python dependencies (installed by the remote build in Azure) |
| `host.json` | Functions host configuration |
| `deploy.sh` | Create/update the Azure resources and publish |
| `run_local.sh` | Run the app locally with the vault values |
| `local.settings.json.example` | Template for local Functions settings (no secrets) |
| `vault/vault.cfg.example` | Template for the settings and token; copy to `vault/vault.cfg` |

## Adding functions

Put `@traced_http` directly above the function, below the `@app.*` decorators:

```python
@app.function_name("name")
@app.route(route="name", methods=["GET"])
@traced_http
def name(req: func.HttpRequest) -> func.HttpResponse:
    ...
```

`@traced_http` is for HTTP triggers. For other triggers (timer, queue, ...) create the span
yourself with `tracer.start_as_current_span(...)` and flush the same way.

## Clean up

To delete only what the deployment created (keep the resource group if it holds other resources):

```bash
az functionapp delete -n dt-otel-az-fun-app -g rg-dt-otel-test
az monitor app-insights component delete --app dt-otel-az-fun-app -g rg-dt-otel-test
az storage account delete -n dtotelazfunappsa -g rg-dt-otel-test
```

If the resource group was created only for this app, delete it instead:
`az group delete -n rg-dt-otel-test`.

Also revoke the Dynatrace access token if it is no longer needed.

## Never commit

- `vault/vault.cfg` (token), already in `.gitignore`
- `.venv/`, `__pycache__/`, `.python_packages/`, `local.settings.json`
