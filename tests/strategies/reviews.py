"""Generated sequences of Review_Triggers, for the properties about what a review costs.

R30 gives a Recovery_Case that chose restraint three ways back into a decision cycle: the
Review_Sweeper finding its ``next_review_at`` due, a subsequent at-risk Payment_Event attaching to
it, and — from task 40.3 — an accepted Customer_Signal. R30.C9 is a claim about all three *at once*:
no trigger of any kind may produce a second enqueued decision cycle while one is unapplied. A test
that drove one kind repeatedly would establish the sweep is idempotent against itself and say
nothing about the case the requirement is actually worded for, which is a trigger of one kind
arriving while another kind's cycle is still pending.

**So the interesting input is a sequence of *kinds*, not a sequence of instants.** The bound being
tested is a uniqueness constraint on a dedupe key, and what breaks it is a second enqueue — from
anywhere — before the first is claimed. Length matters too: two triggers is the smallest case and
finds a missing constraint, while a longer run is what finds a mechanism that dedupes the second
enqueue and then lets the fourth through.

**All three members are drivable since task 40.3.** ``CUSTOMER_SIGNAL`` was generated before its
producer existed and filtered out by :func:`applied_kinds`, precisely so that the sequence shape did
not change when the producer landed — only the filter did, and the counterexamples recorded before
it landed stayed comparable with the ones after. That was the point of generating a member nothing
could execute rather than adding it later.
"""

from __future__ import annotations

from hypothesis import strategies as st

from revora.domain.enums import ReviewTrigger

__all__ = [
    "DRIVABLE_TRIGGERS",
    "applied_kinds",
    "review_trigger_sequences",
    "review_triggers",
]

DRIVABLE_TRIGGERS: tuple[ReviewTrigger, ...] = (
    ReviewTrigger.SCHEDULED_REVIEW,
    ReviewTrigger.EVENT_ATTACHED,
    ReviewTrigger.CUSTOMER_SIGNAL,
)
"""Every trigger something can actually produce. All three, since task 40.3.

``CUSTOMER_SIGNAL`` was absent while it had no producer, and the absence was the seam rather than
an omission: a consumer of this module would otherwise have claimed coverage of a path with no
implementation behind it. Task 40.3 built ``revora.customer.signals.record_signal``, which
enqueues the trigger through the same :func:`~revora.cases.review.enqueue_case_review` the sweeper
and the detection service use, so the member joins the list and :func:`applied_kinds` becomes the
identity.

:func:`applied_kinds` is kept rather than deleted, and that is deliberate. It is where the *next*
undrivable member would be filtered, and its docstring is the record of why a filter existed at
all — a helper deleted the moment it became the identity is a helper somebody has to reinvent the
next time a trigger is declared before its producer exists."""


def review_triggers(*, drivable_only: bool = False) -> st.SearchStrategy[ReviewTrigger]:
    """One Review_Trigger.

    ``drivable_only`` restricts the draw to :data:`DRIVABLE_TRIGGERS`. A consumer that executes the
    trigger against a running system wants that; a consumer that only reasons about the enumeration
    — R30.C11's record shape, say — wants the whole of it.
    """
    return st.sampled_from(DRIVABLE_TRIGGERS if drivable_only else tuple(ReviewTrigger))


def review_trigger_sequences(
    *, min_size: int = 2, max_size: int = 6, drivable_only: bool = True
) -> st.SearchStrategy[tuple[ReviewTrigger, ...]]:
    """A sequence of Review_Triggers to apply to one Recovery_Case, in order.

    ``min_size`` is two because one trigger cannot violate R30.C9 — the requirement is about the
    *second* enqueue, so a single-element sequence passes every assertion vacuously and shrinking
    towards it would report a counterexample that demonstrates nothing.

    ``max_size`` is six rather than unbounded because each element is a real round trip through
    Postgres in the tier that consumes this. Six is enough to interleave both kinds twice over with
    a repeat, and past that the sequences explore no new orderings of two kinds — they just cost
    more per example, which buys fewer examples in the same budget.

    Consecutive repeats are deliberately permitted. ``(SCHEDULED_REVIEW, SCHEDULED_REVIEW)`` is
    R30.C9's second clause almost verbatim — *at most one enqueued cycle from any number of sweeper
    passes over one case whose fields are unchanged* — so filtering repeats out would remove the
    simplest failing case from the search.
    """
    return st.lists(
        review_triggers(drivable_only=drivable_only), min_size=min_size, max_size=max_size
    ).map(tuple)


def applied_kinds(sequence: tuple[ReviewTrigger, ...]) -> tuple[ReviewTrigger, ...]:
    """The subsequence a consumer can actually drive against a running system today.

    The identity since task 40.3, because :data:`DRIVABLE_TRIGGERS` is now the whole enumeration.
    Kept anyway: it was a filter rather than a narrower generator so that a counterexample recorded
    the sequence that was *drawn* — including a ``CUSTOMER_SIGNAL`` that was skipped — and stayed
    comparable with the same counterexample once the member became drivable. That comparability is
    the thing that has to survive, and it survives by this function still existing at the same call
    sites rather than by them being edited.
    """
    return tuple(trigger for trigger in sequence if trigger in DRIVABLE_TRIGGERS)
