"""Structured JSON logging with an ambient correlation id.

R11.C7 requires every audit record produced while processing one inbound event to
carry the same ``correlation_id`` — *including* records produced by work the
processing scheduled asynchronously. Logs follow the same rule, because a log line
you cannot join to the event that caused it is not much use when a merchant asks
why a customer was contacted.

The correlation id lives in a ``contextvars.ContextVar`` rather than being threaded
through every signature. That is not laziness: the alternative is a
``correlation_id`` parameter on every function between the request handler and the
masking serializer, and the one place it gets dropped is the place the trace breaks.
A ``ContextVar`` is inherited by ``asyncio`` tasks automatically, so the API handler
sets it once per request and the worker sets it once per claimed job (the job
payload carries the id and nothing else that identifies anyone — R17.C7).

Every record passes through ``masking.mask_record`` before emission. Field values,
not the message: a message is static text chosen by the programmer, and values are
passed as keyword fields precisely so the serializer can see them by name. A
contact interpolated into a message string is invisible to any serializer, which is
why ``RevoraLogger`` takes ``**fields`` and the convention is that messages are
constant.

``python-json-logger`` is a declared dependency but not used here — one formatter
over ``json.dumps`` is ~40 lines, and P32 asks for a guarantee about what reaches
the output stream, which is easier to hold when the emission path is visible.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any, Final

from revora.platform import clock
from revora.platform.masking import MASK_DISCLOSURE_LENGTH, mask_record

__all__ = [
    "CORRELATION_ID_FIELD",
    "FIELDS_ATTRIBUTE",
    "JsonFormatter",
    "RevoraLogger",
    "clear_correlation_id",
    "configure_logging",
    "correlation_context",
    "current_correlation_id",
    "get_logger",
    "new_correlation_id",
    "set_correlation_id",
]

CORRELATION_ID_FIELD: Final[str] = "correlation_id"
FIELDS_ATTRIBUTE: Final[str] = "revora_fields"
"""``LogRecord`` attribute carrying the structured fields. Namespaced so it cannot
collide with a stdlib ``LogRecord`` attribute and get silently overwritten."""

_UNSET: Final[str] = "unset"
"""Emitted when a log call happens outside any correlation context. Explicit rather
than ``null``, because "nobody set one" and "one was set to nothing" are different
bugs and only one of them is in this process."""

_correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "revora_correlation_id", default=_UNSET
)

_RESERVED_RECORD_ATTRS: Final[frozenset[str]] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
        FIELDS_ATTRIBUTE,
    }
)


def new_correlation_id() -> str:
    """A fresh id, for an occurrence not derived from an inbound event.

    P13's second clause: a scheduled sweep, a manual action or a rejected signature
    has no originating event to inherit from, so it generates one id and every
    record it produces shares it.
    """
    return str(uuid.uuid4())


def current_correlation_id() -> str:
    """The ambient correlation id, or ``"unset"`` outside any context."""
    return _correlation_id.get()


def set_correlation_id(correlation_id: str) -> contextvars.Token[str]:
    """Set the ambient id, returning a token that restores the previous value.

    Called by the API request handler on request and by the worker when it claims a
    job. Returns a token rather than nothing so a nested context cannot clobber an
    outer one.
    """
    return _correlation_id.set(correlation_id)


def clear_correlation_id(token: contextvars.Token[str] | None = None) -> None:
    """Restore the previous id, or clear it entirely if no token is given."""
    if token is None:
        _correlation_id.set(_UNSET)
    else:
        _correlation_id.reset(token)


@contextmanager
def correlation_context(correlation_id: str | None = None) -> Iterator[str]:
    """Bind a correlation id for the duration of the block.

    Generates one if not supplied. The ``finally`` restore matters: a worker that
    leaked the previous job's id into the next job would produce an audit trail
    that attributes one case's actions to another.
    """
    resolved = correlation_id or new_correlation_id()
    token = set_correlation_id(resolved)
    try:
        yield resolved
    finally:
        clear_correlation_id(token)


class JsonFormatter(logging.Formatter):
    """One JSON object per line, masked, with the ambient correlation id attached.

    Timestamps come from ``platform.clock`` rather than ``record.created`` for the
    same reason everything else does: one source of "now", UTC only, and movable
    under test so an assertion on an emitted line is exact.
    """

    def __init__(self, *, disclosure_length: int = MASK_DISCLOSURE_LENGTH) -> None:
        super().__init__()
        self._disclosure_length = disclosure_length

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": clock.now().isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            CORRELATION_ID_FIELD: current_correlation_id(),
        }
        payload.update(self._extra_fields(record))

        # ``LogRecord.exc_info`` is ``False`` rather than ``None`` when a call site
        # passes ``exc_info=False``, so this checks the shape, not truthiness.
        if isinstance(record.exc_info, tuple):
            # Type and message only. A traceback can contain a local variable
            # holding a contact, and the masking serializer cannot see into it.
            exc_type, exc_value = record.exc_info[0], record.exc_info[1]
            payload["error_type"] = exc_type.__name__ if exc_type else "UnknownError"
            payload["error_message"] = str(exc_value) if exc_value else ""

        masked = mask_record(payload, disclosure_length=self._disclosure_length)
        # The correlation id is a generated uuid, never customer-derived, but a
        # field-name registry cannot know that, so it is reinstated after masking
        # to keep the trace joinable.
        masked[CORRELATION_ID_FIELD] = payload[CORRELATION_ID_FIELD]
        return json.dumps(masked, default=_fallback_repr, separators=(",", ":"))

    def _extra_fields(self, record: logging.LogRecord) -> Mapping[str, Any]:
        fields: dict[str, Any] = {}
        declared = getattr(record, FIELDS_ATTRIBUTE, None)
        if isinstance(declared, Mapping):
            fields.update(declared)
        # Anything attached via logging's own `extra=` is picked up too, so a
        # third-party call site cannot bypass masking by using the stdlib API.
        for key, value in record.__dict__.items():
            if key not in _RESERVED_RECORD_ATTRS and not key.startswith("_"):
                fields.setdefault(key, value)
        return fields


def _fallback_repr(value: object) -> str:
    """Last resort for a value ``json`` cannot encode.

    ``repr`` rather than ``str`` for one reason: ``SecretValue.__repr__`` redacts,
    and anything else reaching here is a programming mistake worth seeing in full.
    """
    return repr(value)


def configure_logging(
    *,
    level: int = logging.INFO,
    stream: Any = None,
    disclosure_length: int = MASK_DISCLOSURE_LENGTH,
) -> logging.Handler:
    """Install the JSON formatter on the root logger, replacing existing handlers.

    Replacing rather than adding: a second handler with the default formatter would
    emit the same record unmasked next to the masked one, which is the exact leak
    P32 forbids.
    """
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(JsonFormatter(disclosure_length=disclosure_length))
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)
    return handler


class RevoraLogger:
    """Thin wrapper that forces values into fields instead of into the message.

    ``logger.info("link created", provider_short_url=url)`` is maskable.
    ``logger.info(f"link created: {url}")`` is not. The wrapper takes no positional
    interpolation arguments, which makes the second form awkward enough to notice
    in review.
    """

    __slots__ = ("_logger",)

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def debug(self, message: str, **fields: Any) -> None:
        self._log(logging.DEBUG, message, fields)

    def info(self, message: str, **fields: Any) -> None:
        self._log(logging.INFO, message, fields)

    def warning(self, message: str, **fields: Any) -> None:
        self._log(logging.WARNING, message, fields)

    def error(self, message: str, *, exc_info: bool = False, **fields: Any) -> None:
        self._log(logging.ERROR, message, fields, exc_info=exc_info)

    def exception(self, message: str, **fields: Any) -> None:
        self._log(logging.ERROR, message, fields, exc_info=True)

    def _log(
        self,
        level: int,
        message: str,
        fields: Mapping[str, Any],
        *,
        exc_info: bool = False,
    ) -> None:
        self._logger.log(
            level,
            message,
            extra={FIELDS_ATTRIBUTE: dict(fields)},
            exc_info=exc_info or None,
        )

    def __repr__(self) -> str:
        return f"RevoraLogger({self._logger.name!r})"


def get_logger(name: str) -> RevoraLogger:
    """A logger for ``name``. Use the module's ``__name__``."""
    return RevoraLogger(logging.getLogger(name))
