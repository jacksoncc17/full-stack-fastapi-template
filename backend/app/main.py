from pathlib import Path

import sentry_sdk
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import ValidationError
from starlette.middleware.cors import CORSMiddleware

from app.api.main import api_router
from app.core.config import settings

FRONTEND_DIR = Path(__file__).parent / "frontend"


def custom_generate_unique_id(route: APIRoute) -> str:
    return f"{route.tags[0]}-{route.name}"


if settings.SENTRY_DSN and settings.FASTAPI_ENV != "development":
    sentry_sdk.init(dsn=str(settings.SENTRY_DSN), enable_tracing=True)

app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    generate_unique_id_function=custom_generate_unique_id,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.FRONTEND_HOST],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix=settings.API_V1_STR)


@app.exception_handler(ValidationError)
async def _pydantic_validation_handler(request: Request,
                                       exc: ValidationError) -> JSONResponse:
    """Return 422 for a model built inside a route.

    FastAPI converts a ValidationError raised while binding a request body, but NOT
    one raised by a model the route constructs itself -- that surfaced as a 500 for
    something as ordinary as duration_s=9999 on the upload endpoint. A client error
    must not look like a server fault, and 500s also hide the real message in the
    logs rather than telling the caller what to fix.
    """
    return JSONResponse(
        status_code=422,
        content={"detail": exc.errors(include_url=False, include_input=False)},
    )
app.frontend("/", directory=FRONTEND_DIR)
