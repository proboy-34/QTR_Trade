from fastapi import Request
from fastapi.responses import JSONResponse


class QTRError(Exception):
    code = "QTR_ERROR"
    status_code = 400

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFoundError(QTRError):
    code = "NOT_FOUND"
    status_code = 404


class SafetyError(QTRError):
    code = "SAFETY_REJECTION"
    status_code = 409


async def qtr_error_handler(_: Request, exc: QTRError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
    )

