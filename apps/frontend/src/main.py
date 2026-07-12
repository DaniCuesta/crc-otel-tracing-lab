import logging
import os
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
    OTLPSpanExporter,
)
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


BASE_DIR = Path(__file__).resolve().parent

BACKEND_URL = os.getenv(
    "BACKEND_URL",
    "http://backend:8080",
)

BACKEND_TIMEOUT = float(
    os.getenv("BACKEND_TIMEOUT", "10")
)

OTEL_SERVICE_NAME = os.getenv(
    "OTEL_SERVICE_NAME",
    "frontend",
)

OTEL_SERVICE_NAMESPACE = os.getenv(
    "OTEL_SERVICE_NAMESPACE",
    "otel-lab",
)

OTEL_DEPLOYMENT_ENVIRONMENT = os.getenv(
    "OTEL_DEPLOYMENT_ENVIRONMENT",
    "crc",
)

OTEL_EXPORTER_OTLP_ENDPOINT = os.getenv(
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "http://otel-collector:4318",
).rstrip("/")


class TraceContextFilter(logging.Filter):
    """
    Añade trace_id y span_id al LogRecord actual.

    Cuando el log no pertenece a ningún span activo, ambos valores
    aparecen como cadenas de ceros.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        span = trace.get_current_span()
        span_context = span.get_span_context()

        if span_context.is_valid:
            record.trace_id = format(
                span_context.trace_id,
                "032x",
            )
            record.span_id = format(
                span_context.span_id,
                "016x",
            )
        else:
            record.trace_id = "0" * 32
            record.span_id = "0" * 16

        return True


def configure_logging() -> logging.Logger:
    """
    Configura logs técnicos con Trace ID y Span ID.
    """

    handler = logging.StreamHandler()
    handler.addFilter(TraceContextFilter())

    formatter = logging.Formatter(
        "%(asctime)s "
        "level=%(levelname)s "
        "service=frontend "
        "trace_id=%(trace_id)s "
        "span_id=%(span_id)s "
        "message=%(message)s"
    )

    handler.setFormatter(formatter)

    application_logger = logging.getLogger("frontend")
    application_logger.setLevel(logging.INFO)
    application_logger.handlers.clear()
    application_logger.addHandler(handler)
    application_logger.propagate = False

    return application_logger


logger = configure_logging()


def configure_tracing() -> TracerProvider:
    """
    Configura el proveedor de trazas y el exportador OTLP.
    """

    resource = Resource.create(
        {
            "service.name": OTEL_SERVICE_NAME,
            "service.namespace": OTEL_SERVICE_NAMESPACE,
            "deployment.environment.name": (
                OTEL_DEPLOYMENT_ENVIRONMENT
            ),
        }
    )

    provider = TracerProvider(
        resource=resource,
    )

    exporter = OTLPSpanExporter(
        endpoint=(
            f"{OTEL_EXPORTER_OTLP_ENDPOINT}"
            "/v1/traces"
        ),
    )

    processor = BatchSpanProcessor(
        exporter,
    )

    provider.add_span_processor(
        processor,
    )

    trace.set_tracer_provider(
        provider,
    )

    return provider


tracer_provider = configure_tracing()


app = FastAPI(
    title="OTel Demo Shop Frontend",
    description="Frontend del laboratorio OpenTelemetry",
    version="1.0.0",
)

app.mount(
    "/static",
    StaticFiles(
        directory=BASE_DIR / "static",
    ),
    name="static",
)

templates = Jinja2Templates(
    directory=BASE_DIR / "templates",
)


FastAPIInstrumentor.instrument_app(
    app,
    tracer_provider=tracer_provider,
    excluded_urls="health",
)

HTTPXClientInstrumentor().instrument(
    tracer_provider=tracer_provider,
)


GENERIC_MESSAGES = {
    "connection_error": (
        "No se ha podido conectar con el servicio. "
        "Inténtalo de nuevo en unos instantes."
    ),
    "backend_error": (
        "No ha sido posible completar la operación. "
        "Inténtalo de nuevo más tarde."
    ),
    "sql_error": (
        "Se ha producido un problema interno al consultar "
        "la información."
    ),
    "unexpected_error": (
        "Se ha producido un error inesperado. "
        "Inténtalo de nuevo más tarde."
    ),
    "latency_success": (
        "La operación ha tardado más de lo habitual, "
        "pero se ha completado correctamente."
    ),
    "normal_success": (
        "La operación se ha completado correctamente."
    ),
}


def base_context(
    request: Request,
) -> dict[str, Any]:
    """
    Contexto base utilizado por la plantilla.
    """

    return {
        "request": request,
        "products": [],
        "error": None,
        "success_message": None,
        "scenario_result": None,
        "backend_url": BACKEND_URL,
    }


async def call_backend(
    path: str,
) -> dict[str, Any]:
    """
    Ejecuta una llamada HTTP al backend.

    La instrumentación HTTPX crea automáticamente un span CLIENT
    y añade la cabecera traceparent.
    """

    url = f"{BACKEND_URL}{path}"

    logger.info(
        "Calling backend url=%s",
        url,
    )

    async with httpx.AsyncClient(
        timeout=BACKEND_TIMEOUT,
    ) as client:
        response = await client.get(
            url,
        )

        logger.info(
            "Backend response url=%s status=%s",
            url,
            response.status_code,
        )

        response.raise_for_status()

        return response.json()


@app.get(
    "/health",
    include_in_schema=False,
)
async def health() -> dict[str, str]:
    """
    Endpoint para readiness y liveness probes.
    """

    return {
        "status": "ok",
        "service": OTEL_SERVICE_NAME,
    }


@app.get(
    "/",
    response_class=HTMLResponse,
)
async def home(
    request: Request,
) -> HTMLResponse:
    """
    Carga inicial de la página.
    """

    context = base_context(
        request,
    )

    logger.info(
        "Loading product catalogue"
    )

    try:
        response = await call_backend(
            "/products",
        )

        context["products"] = response.get(
            "products",
            [],
        )

        logger.info(
            "Product catalogue loaded count=%s",
            len(context["products"]),
        )

    except httpx.HTTPStatusError as exc:
        logger.exception(
            "Backend returned HTTP error "
            "status=%s body=%s",
            exc.response.status_code,
            exc.response.text,
        )

        context["error"] = (
            GENERIC_MESSAGES["backend_error"]
        )

    except httpx.RequestError:
        logger.exception(
            "Unable to connect to backend "
            "backend_url=%s",
            BACKEND_URL,
        )

        context["error"] = (
            GENERIC_MESSAGES["connection_error"]
        )

    except Exception:
        logger.exception(
            "Unexpected error while loading "
            "product catalogue"
        )

        context["error"] = (
            GENERIC_MESSAGES["unexpected_error"]
        )

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context=context,
    )


@app.get(
    "/scenario/{scenario}",
    response_class=HTMLResponse,
)
async def run_scenario(
    request: Request,
    scenario: str,
) -> HTMLResponse:
    """
    Ejecuta los escenarios de prueba.
    """

    scenarios = {
        "normal": {
            "path": "/products",
            "success_message": (
                GENERIC_MESSAGES["normal_success"]
            ),
        },
        "latency": {
            "path": "/simulate-latency",
            "success_message": (
                GENERIC_MESSAGES["latency_success"]
            ),
        },
        "error": {
            "path": "/simulate-error",
            "success_message": None,
        },
        "sql-error": {
            "path": "/simulate-sql-error",
            "success_message": None,
        },
    }

    context = base_context(
        request,
    )

    selected_scenario = scenarios.get(
        scenario,
    )

    if selected_scenario is None:
        logger.warning(
            "Unknown scenario requested "
            "scenario=%s",
            scenario,
        )

        context["error"] = (
            GENERIC_MESSAGES["unexpected_error"]
        )

        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context=context,
            status_code=400,
        )

    logger.info(
        "Starting scenario scenario=%s",
        scenario,
    )

    try:
        response = await call_backend(
            selected_scenario["path"],
        )

        context["scenario_result"] = response
        context["products"] = response.get(
            "products",
            [],
        )
        context["success_message"] = (
            selected_scenario["success_message"]
        )

        logger.info(
            "Scenario completed scenario=%s",
            scenario,
        )

    except httpx.HTTPStatusError as exc:
        logger.exception(
            "Scenario returned HTTP error "
            "scenario=%s status=%s body=%s",
            scenario,
            exc.response.status_code,
            exc.response.text,
        )

        if scenario == "sql-error":
            context["error"] = (
                GENERIC_MESSAGES["sql_error"]
            )
        else:
            context["error"] = (
                GENERIC_MESSAGES["backend_error"]
            )

    except httpx.RequestError:
        logger.exception(
            "Scenario connection failure "
            "scenario=%s backend_url=%s",
            scenario,
            BACKEND_URL,
        )

        context["error"] = (
            GENERIC_MESSAGES["connection_error"]
        )

    except Exception:
        logger.exception(
            "Unexpected scenario failure "
            "scenario=%s",
            scenario,
        )

        context["error"] = (
            GENERIC_MESSAGES["unexpected_error"]
        )

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context=context,
    )


@app.on_event("shutdown")
async def shutdown_tracer_provider() -> None:
    """
    Fuerza el envío de spans pendientes antes de detener el proceso.
    """

    tracer_provider.force_flush(
        timeout_millis=5000,
    )

    tracer_provider.shutdown()