"""Crash plans: where a worker dies during an execution attempt, and what happens next.

Property 3 is a claim about a process that stops existing at an inconvenient moment. It
cannot be checked by calling functions in order and inspecting the return values, because
the interesting behaviour is what *did not* get written. So this module makes a crash a
first-class generated input: a :class:`CrashPlan` says where the worker dies, what the
provider does either side of that, and how many times the worker restarts afterwards.

**Crashes are injected without a single test-only branch in production code.** That
constraint is not tidiness. A crash hook compiled into the execution engine would be a
code path that only tests exercise, sitting in the one function whose correctness the
whole system rests on, and the version of the engine under test would not be the version
that ships. Two mechanisms make hooks unnecessary:

* The two crash points either side of the provider call are enacted by a
  :class:`PaymentProviderClient` that raises instead of returning. That is not a hook — it
  is an implementation of the protocol the engine already depends on, which is exactly
  what the protocol exists for.
* The two crash points at transaction boundaries are enacted by a SQLAlchemy
  ``after_cursor_execute`` listener that fires on the statement that writes the intent.
  The engine is untouched; the database connection dies under it, which is what a real
  crash looks like from the engine's point of view.

**Why the crash exception derives from** ``BaseException``. ``RazorpayClient`` ends in a
deliberate catch-all — ``except Exception`` becoming ``Unclassifiable`` — so that no
provider failure can ever escape as a raise. A crash injected as an ``Exception`` subclass
would be swallowed by that catch-all and quietly reclassified, and the test would assert
against an orderly result instead of a dead worker. Deriving from ``BaseException`` makes
the injected crash behave the way a killed process behaves: nothing catches it, and every
open transaction rolls back untouched.

**The crash is one-shot.** Each mechanism disarms after firing once, because the property
is about a worker that dies *and then recovers*. A crash that fired on every attempt would
make the assertions trivially true — no calls, no effects, nothing to double.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import StrEnum, unique
from typing import TYPE_CHECKING, Any, Final

from hypothesis import strategies as st
from sqlalchemy import event

from revora.providers.classification import (
    PaymentEntity,
    PaymentLinkEntity,
    PaymentLinkList,
    PaymentLinkResendAck,
    PaymentList,
    ProviderResult,
)
from revora.providers.payment_link import NotifyMedium, PaymentLinkRequest
from tests.fakes.razorpay import (
    CreateOutcome,
    FakeRazorpay,
    ProviderBehaviour,
    ResendOutcome,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.engine import Engine

__all__ = [
    "CrashInjected",
    "CrashPlan",
    "CrashPoint",
    "CrashingProvider",
    "crash_on_statement",
    "crash_plan",
    "execution_crash_plan",
    "resend_crash_plan",
]


class CrashInjected(BaseException):
    """A simulated process death.

    ``BaseException``, not ``Exception``, and that is the whole point — see the module
    docstring. If this were catchable by a bare ``except Exception`` it would be caught,
    because the provider client has one on purpose.
    """

    def __init__(self, point: CrashPoint) -> None:
        self.point = point
        super().__init__(f"simulated crash at {point.value}")


@unique
class CrashPoint(StrEnum):
    """Where the worker stops existing.

    The four points are not arbitrary: they are the four gaps in the ten-step execution
    protocol where the durable record and the external world can disagree. Ordered by
    how much damage a naive implementation does at each.
    """

    NONE = "NONE"
    """The control. Without it the property could pass because nothing ever ran."""

    BEFORE_INTENT_COMMIT = "BEFORE_INTENT_COMMIT"
    """The insert reached the database; the transaction never committed. Nothing durable,
    nothing external. A correct retry starts completely fresh and must still end with one
    effect, not zero — this is the point that catches an engine which loses the work."""

    AFTER_INTENT_COMMIT_BEFORE_CALL = "AFTER_INTENT_COMMIT_BEFORE_CALL"
    """The intent is durable as ``ATTEMPTED`` and no call was made. The dangerous
    reading is "``ATTEMPTED`` means in flight, so wait" — which strands the case forever —
    and the other dangerous reading is "no provider id, so call again", which is safe here
    but indistinguishable from the next point, where it duplicates the effect."""

    AFTER_CALL_BEFORE_RESULT_COMMIT = "AFTER_CALL_BEFORE_RESULT_COMMIT"
    """The call went out and the result was never recorded. The provider may or may not
    have created the link and the durable record cannot tell which. Any implementation
    that retries the create here will one day charge a customer twice."""

    DURING_RECONCILIATION = "DURING_RECONCILIATION"
    """The worker died resolving an earlier crash. Reconciliation must be as re-runnable
    as the thing it reconciles, or the recovery path becomes the bug."""


@dataclass(frozen=True, slots=True)
class CrashPlan:
    """One generated scenario: a crash, the provider's behaviour, and the recovery.

    ``restarts`` and ``reconciliation_runs`` are part of the plan rather than fixed,
    because "at most one effect" has to hold across *any* number of recovery attempts, not
    just one. An implementation that is idempotent once and not twice is not idempotent.
    """

    point: CrashPoint
    behaviour: ProviderBehaviour
    restarts: int
    reconciliation_runs: int

    @property
    def crashes_at_a_transaction_boundary(self) -> bool:
        """Whether this plan is enacted by the SQL listener rather than the provider."""
        return self.point in (
            CrashPoint.BEFORE_INTENT_COMMIT,
            CrashPoint.DURING_RECONCILIATION,
        )

    @property
    def crashes_around_the_call(self) -> bool:
        """Whether this plan is enacted by :class:`CrashingProvider`."""
        return self.point in (
            CrashPoint.AFTER_INTENT_COMMIT_BEFORE_CALL,
            CrashPoint.AFTER_CALL_BEFORE_RESULT_COMMIT,
        )

    @property
    def effect_may_exist_after_the_crash(self) -> bool:
        """Whether the provider could have created the link before the worker died.

        Only true where the crash is at or after the call *and* the scripted behaviour
        actually creates something. Used by the test to decide which assertions are even
        meaningful — not to weaken them.
        """
        if not self.crashes_around_the_call:
            return self.point is not CrashPoint.BEFORE_INTENT_COMMIT
        return self.point is CrashPoint.AFTER_CALL_BEFORE_RESULT_COMMIT


# ---------------------------------------------------------------------------
# The strategy
# ---------------------------------------------------------------------------

_INTERESTING_OUTCOMES: Final[tuple[CreateOutcome, ...]] = (
    CreateOutcome.SUCCESS,
    CreateOutcome.TIMEOUT_EFFECT_CREATED,
    CreateOutcome.TIMEOUT_NO_EFFECT,
    CreateOutcome.SERVER_ERROR,
    CreateOutcome.CLIENT_ERROR,
    CreateOutcome.UNCLASSIFIABLE,
)
"""The create outcomes worth crossing with a crash.

``TIMEOUT_EFFECT_CREATED`` and ``TIMEOUT_NO_EFFECT`` are the pair that matters: they are
indistinguishable to the caller and differ only in whether the effect exists. A plan that
crashes after the call, with the provider having created a link the engine never heard
about, is the precise scenario Property 3 exists for. ``CONNECT_ERROR`` is left out
because it is definitively "not sent" and the retry-once behaviour is already covered by
the provider's own unit tests."""


def crash_plan() -> st.SearchStrategy[CrashPlan]:
    """Every crash point, crossed with a provider that behaves in an interesting way.

    Recovery attempts are bounded at three because the reconciliation ceiling is six and
    the property has to hold below, at and above the point where reconciliation gives up.
    Larger numbers buy no new behaviour and cost a database round trip each.
    """
    return st.builds(
        CrashPlan,
        point=st.sampled_from(CrashPoint),
        behaviour=st.builds(
            ProviderBehaviour,
            create_outcomes=st.lists(
                st.sampled_from(_INTERESTING_OUTCOMES), min_size=1, max_size=2
            ).map(tuple),
            listing_empty_reads=st.integers(min_value=0, max_value=2),
            listing_unavailable_reads=st.integers(min_value=0, max_value=1),
        ),
        restarts=st.integers(min_value=1, max_value=3),
        reconciliation_runs=st.integers(min_value=0, max_value=3),
    )


_INTERESTING_RESEND_OUTCOMES: Final[tuple[ResendOutcome, ...]] = (
    ResendOutcome.SUCCESS,
    ResendOutcome.RATE_LIMITED,
    ResendOutcome.TIMEOUT,
    ResendOutcome.SERVER_ERROR,
    ResendOutcome.CLIENT_ERROR,
    ResendOutcome.ACK_FALSE,
)
"""The resend outcomes worth crossing with a crash, one per settled disposition.

``SUCCESS`` confirms, ``RATE_LIMITED`` and ``CLIENT_ERROR`` fail definitively, ``TIMEOUT``,
``SERVER_ERROR`` and ``ACK_FALSE`` land ``UNCERTAIN`` — which for a resend is terminal, and is
therefore the outcome under which "no second call, ever" is hardest to hold. ``UNPARSEABLE``
classifies exactly as ``ACK_FALSE`` does and ``CONNECT_ERROR`` exactly as ``CLIENT_ERROR`` does,
so both buy a database round trip and no new behaviour."""


def resend_crash_plan() -> st.SearchStrategy[CrashPlan]:
    """:func:`crash_plan` with the resend outcomes varied instead of the create outcomes.

    A separate strategy rather than a widened one, because the two paths are not
    interchangeable: a create's plan wants ``TIMEOUT_EFFECT_CREATED`` against
    ``TIMEOUT_NO_EFFECT``, and the resend has no such pair to draw — nothing reports whether a
    notification was sent, so the fake cannot offer the distinction and a test must not assume
    it. ``reconciliation_runs`` stays in the plan and is expected to change nothing at all: a
    resend row is absent from the sweep's candidate set, so a plan that runs it three times is
    a plan that proves the absence.
    """
    return st.builds(
        CrashPlan,
        point=st.sampled_from(CrashPoint),
        behaviour=st.builds(
            ProviderBehaviour,
            resend_outcomes=st.lists(
                st.sampled_from(_INTERESTING_RESEND_OUTCOMES), min_size=1, max_size=2
            ).map(tuple),
        ),
        restarts=st.integers(min_value=1, max_value=3),
        reconciliation_runs=st.integers(min_value=0, max_value=2),
    )


def execution_crash_plan() -> st.SearchStrategy[CrashPlan]:
    """:func:`crash_plan` restricted to plans that actually reach the provider.

    For the assertions that are only meaningful once an attempt was made. Kept separate
    rather than filtered inline so a reader can see that the unrestricted strategy is the
    one the headline property uses.
    """
    return crash_plan().filter(lambda plan: plan.point is not CrashPoint.NONE)


# ---------------------------------------------------------------------------
# Enacting a crash around the provider call
# ---------------------------------------------------------------------------


class CrashingProvider:
    """A provider client that dies once, either side of the create call.

    Satisfies ``providers.razorpay.PaymentProviderClient`` structurally. Every read
    operation delegates untouched: the crash belongs to the write, because a read that
    dies costs nothing but a retry.
    """

    __slots__ = ("_armed", "_inner", "_point")

    def __init__(self, inner: FakeRazorpay, point: CrashPoint) -> None:
        self._inner = inner
        self._point = point
        self._armed = point in (
            CrashPoint.AFTER_INTENT_COMMIT_BEFORE_CALL,
            CrashPoint.AFTER_CALL_BEFORE_RESULT_COMMIT,
        )

    @property
    def fired(self) -> bool:
        """Whether the crash has been spent. False for a plan that never arms one."""
        return not self._armed and self._point in (
            CrashPoint.AFTER_INTENT_COMMIT_BEFORE_CALL,
            CrashPoint.AFTER_CALL_BEFORE_RESULT_COMMIT,
        )

    def create_payment_link(
        self, request: PaymentLinkRequest
    ) -> ProviderResult[PaymentLinkEntity]:
        """Delegate, dying before or after depending on the plan.

        Dying *after* delegating is the important one: the fake has already decided
        whether the link exists, so the external world has moved while the engine's record
        has not.
        """
        if self._armed and self._point is CrashPoint.AFTER_INTENT_COMMIT_BEFORE_CALL:
            self._armed = False
            raise CrashInjected(self._point)

        result = self._inner.create_payment_link(request)

        if self._armed and self._point is CrashPoint.AFTER_CALL_BEFORE_RESULT_COMMIT:
            self._armed = False
            raise CrashInjected(self._point)

        return result

    def notify_by(
        self, payment_link_id: str, medium: NotifyMedium
    ) -> ProviderResult[PaymentLinkResendAck]:
        """The resend, dying on the same two sides as the create.

        Present because the crash points are a property of the protocol, not of one operation:
        ``PROMISE_TO_PAY_FOLLOW_UP`` reaches the provider through this method, so a Property 44
        run against a client that lacked it would be a run against a client the engine cannot
        use. The asymmetry with the create is not here but downstream — an effect this method
        may have had is unreadable afterwards, which is why the property it serves asserts *no
        second call ever* rather than *reconciled to one*.
        """
        if self._armed and self._point is CrashPoint.AFTER_INTENT_COMMIT_BEFORE_CALL:
            self._armed = False
            raise CrashInjected(self._point)

        result = self._inner.notify_by(payment_link_id, medium)

        if self._armed and self._point is CrashPoint.AFTER_CALL_BEFORE_RESULT_COMMIT:
            self._armed = False
            raise CrashInjected(self._point)

        return result

    def find_payment_links_by_reference_id(
        self, reference_id: str
    ) -> ProviderResult[PaymentLinkList]:
        return self._inner.find_payment_links_by_reference_id(reference_id)

    def fetch_payment(self, payment_id: str) -> ProviderResult[PaymentEntity]:
        return self._inner.fetch_payment(payment_id)

    def list_payments(
        self, *, from_ts: int, to_ts: int, count: int = 100, skip: int = 0
    ) -> ProviderResult[PaymentList]:
        return self._inner.list_payments(
            from_ts=from_ts, to_ts=to_ts, count=count, skip=skip
        )

    def __repr__(self) -> str:
        return f"CrashingProvider(point={self._point.value}, armed={self._armed})"


# ---------------------------------------------------------------------------
# Enacting a crash at a transaction boundary
# ---------------------------------------------------------------------------

_INTENT_INSERT = re.compile(r"insert\s+into\s+execution_intent", re.IGNORECASE)
_INTENT_UPDATE = re.compile(r"update\s+execution_intent", re.IGNORECASE)

_STATEMENT_FOR_POINT: Final[dict[CrashPoint, re.Pattern[str]]] = {
    CrashPoint.BEFORE_INTENT_COMMIT: _INTENT_INSERT,
    CrashPoint.DURING_RECONCILIATION: _INTENT_UPDATE,
}


@contextmanager
def crash_on_statement(engine: Engine, point: CrashPoint) -> Iterator[list[str]]:
    """Die once, immediately after the statement that writes the intent.

    ``after_cursor_execute`` rather than ``before``: the statement must reach the database
    inside the transaction and *then* be lost, because that is the state a crash actually
    leaves behind. Firing before the execute would test a weaker thing — an attempt that
    never touched the row at all.

    Matching on the SQL text is deliberately loose. The alternative is to assert on how
    the engine issues its writes, and this listener would then have to be rewritten every
    time the engine's internals change, which is precisely backwards for a test whose job
    is to survive implementation churn. The pattern only has to be specific enough to name
    the intent row, and it is.

    Yields the list of statements that triggered a crash, so a test can confirm the crash
    it asked for was actually enacted rather than silently skipped.
    """
    pattern = _STATEMENT_FOR_POINT.get(point)
    fired: list[str] = []
    if pattern is None:
        yield fired
        return

    def _after_cursor_execute(
        _conn: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if fired or not pattern.search(statement):
            return
        fired.append(statement)
        raise CrashInjected(point)

    event.listen(engine, "after_cursor_execute", _after_cursor_execute)
    try:
        yield fired
    finally:
        event.remove(engine, "after_cursor_execute", _after_cursor_execute)


def without_crash(plan: CrashPlan) -> CrashPlan:
    """The same plan with the crash removed, for the recovery phase of a test."""
    return replace(plan, point=CrashPoint.NONE)
