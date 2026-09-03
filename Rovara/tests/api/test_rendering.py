"""The presentation contract, checked without a database.

Everything here is a statement about what a value looks like on the wire, and none of it needs a
server — so it runs in the fast tier, on every commit, rather than in the tier that needs Postgres.
That split is deliberate: these are the rules a refactor is most likely to break and least likely
to notice breaking, because a wrong money string still renders.

The degraded-metrics case is here rather than only in the integration tier because forcing a real
statement timeout is machine-dependent. Building the document directly checks the *contract*
deterministically; the integration test checks the *plumbing* opportunistically. Neither alone is
enough and neither is flaky.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from revora.api.rendering import (
    DATA_UNAVAILABLE,
    NOT_YET_RECORDED,
    PRESENT,
    data_unavailable,
    grouping_for,
    money,
    not_yet_recorded,
    rate,
    symbol_for,
)
from revora.api.views import NULL_SELECTION_REASONS, metrics_document
from revora.domain.actions import EXECUTABLE_ACTIONS
from revora.domain.enums import NOT_ESTABLISHED, UNDEFINED, SelectionReason
from revora.domain.money import INDIAN_GROUPING, WESTERN_GROUPING
from revora.metrics.engine import (
    CohortMetrics,
    IncrementalFinding,
    ReportingPeriod,
    SegmentKey,
)

pytestmark = pytest.mark.pure


def _metrics(*, incremental: IncrementalFinding, labels: tuple[str, ...]) -> CohortMetrics:
    """A cohort report with plausible figures, for exercising the serializer alone."""
    return CohortMetrics(
        period=ReportingPeriod(
            start=datetime(2026, 1, 1, tzinfo=UTC), end=datetime(2026, 2, 1, tzinfo=UTC)
        ),
        computed_at=datetime(2026, 2, 1, 12, tzinfo=UTC),
        segment=SegmentKey(),
        case_count=40,
        revenue_at_risk=8_000_000,
        recovered_case_count=9,
        observed_recovered_revenue=1_800_000,
        natural_recovered_revenue=1_100_000,
        total_recovery_cost=12_000,
        unresolved_revenue=6_200_000,
        # R31.C12's four terms. Distinct values, so a serializer that emitted one of them
        # twice or put the sum in a term's slot would show up as a wrong number rather
        # than as a coincidence.
        financial_cost=4_800,
        communication_cost=400,
        risk_cost=0,
        customer_cost=16_000,
        blocked_case_count=3,
        escalated_case_count=1,
        unnecessary_action_count=0,
        intervened_case_count=14,
        confirmed_action_count=16,
        successful_action_count=6,
        cycles_without_action_count=22,
        recovery_rate=Decimal("0.2250"),
        intervention_rate=Decimal("0.3500"),
        action_success_rate=Decimal("0.3750"),
        escalation_rate=Decimal("0.0250"),
        average_hours_to_recovery=Decimal("18.5000"),
        incremental=incremental,
        labels=labels,
    )


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("minor", "expected"),
    [
        (0, "₹0.00"),
        (1, "₹0.01"),
        (99, "₹0.99"),
        (100, "₹1.00"),
        (250_000, "₹2,500.00"),
        (2_000_000, "₹20,000.00"),
        (123_456_789, "₹12,34,567.89"),
        (-250_000, "-₹2,500.00"),
    ],
)
def test_money_renders_with_grouping_and_two_minor_digits(minor: int, expected: str) -> None:
    """The integer and the string travel together and must agree.

    ``123456789`` is in the list because it is where a naive implementation goes wrong in a way
    nobody notices in testing and everybody notices in production. The negative case is there
    because an incremental figure can legitimately be negative, and a sign rendered inside the
    symbol reads as a currency nobody has heard of.
    """
    field = money(minor, currency="INR")
    assert field["status"] == PRESENT
    assert field["minor"] == minor
    assert field["formatted"] == expected
    assert field["currency"] == "INR"


def test_an_unknown_currency_renders_its_code_rather_than_a_wrong_symbol() -> None:
    """An unfamiliar-looking amount is a much smaller problem than one labelled as another
    country's money."""
    assert symbol_for("XYZ") == "XYZ "
    assert money(250_000, currency="xyz")["formatted"] == "XYZ 2,500.00"


def test_rupees_group_by_lakh_and_everything_else_by_thousand() -> None:
    """The grouping follows the currency, and getting it wrong is not a cosmetic bug.

    ``₹1,234,567.89`` forces an Indian merchant to mentally regroup the number to find the lakh
    figure — and the digit that moves under regrouping is the one that matters most. This was wrong
    until the money test above caught it.
    """
    assert grouping_for("INR") == INDIAN_GROUPING
    assert grouping_for("usd") == WESTERN_GROUPING
    assert money(123_456_789, currency="INR")["formatted"] == "₹12,34,567.89"
    assert money(123_456_789, currency="USD")["formatted"] == "$1,234,567.89"
    # One crore of rupees is 1_000_000_000 paise, and it renders as ₹1,00,00,000.00 — the point
    # where the two conventions diverge most visibly, and where the expectation in the first draft
    # of this test was itself off by a factor of ten.
    assert money(1_000_000_000, currency="INR")["formatted"] == "₹1,00,00,000.00"
    assert money(1_000_000_000, currency="USD")["formatted"] == "$10,000,000.00"


def test_an_absent_amount_is_a_marker_and_carries_no_value_of_any_kind() -> None:
    """R14.C15. Not zero, not null, not a dash — and specifically not a ``minor`` field.

    The absence of ``minor`` is the load-bearing assertion. A marker that also carried
    ``"minor": 0`` would let a client read the number and ignore the status, which is exactly what
    a client under deadline does.
    """
    marker = money(None, currency="INR", absent_state="DETECTED")
    assert marker["status"] == NOT_YET_RECORDED
    assert marker["case_state"] == "DETECTED"
    assert "minor" not in marker
    assert "formatted" not in marker


# ---------------------------------------------------------------------------
# Rates
# ---------------------------------------------------------------------------


def test_a_rate_is_always_a_string_so_the_sentinel_cannot_coerce_to_zero() -> None:
    """``UNDEFINED`` and ``0.0000`` must never be confusable, and JSON numbers make them so."""
    assert rate(Decimal("0.0000")) == {"status": PRESENT, "value": "0.0000"}
    assert rate(UNDEFINED) == {"status": PRESENT, "value": "UNDEFINED"}
    assert rate(None, absent_state="DETECTED")["status"] == NOT_YET_RECORDED


def test_a_data_unavailable_marker_names_the_figure_and_nothing_else() -> None:
    """R14.C16. Scoped to one figure, so the rest of a response still returns."""
    marker = data_unavailable("incremental_recovered_revenue", "the analysis timed out")
    assert marker["status"] == DATA_UNAVAILABLE
    assert marker["figure"] == "incremental_recovered_revenue"


def test_a_not_yet_recorded_marker_explains_itself_with_the_case_state() -> None:
    marker = not_yet_recorded("BLOCKED", "recommendation")
    assert "BLOCKED" in str(marker["detail"])
    assert marker["case_state"] == "BLOCKED"


# ---------------------------------------------------------------------------
# The metrics document
# ---------------------------------------------------------------------------


def test_an_unestablished_incremental_figure_is_the_sentinel_with_its_reasons() -> None:
    """R12.C13. The string sentinel, not a number and not a null.

    ``amount`` is asserted absent for the same reason ``minor`` is absent from a money marker: a
    figure a client can read is a figure a client will read.
    """
    metrics = _metrics(
        incremental=IncrementalFinding(
            value=NOT_ESTABLISHED, refusal_codes=("NO_COMPLETED_EXPERIMENT",)
        ),
        labels=("RECOVERY_GROSS_OF_REFUNDS", "CAUSALITY_NOT_ESTABLISHED"),
    )
    document = metrics_document(metrics, currency="INR")
    incremental = document["incremental_recovered_revenue"]
    assert isinstance(incremental, dict)
    assert incremental["status"] == "NOT_ESTABLISHED"
    assert incremental["refusal_codes"] == ["NO_COMPLETED_EXPERIMENT"]
    assert "amount" not in incremental
    assert document["causality_established"] is False


def test_an_established_incremental_figure_travels_with_the_comparison_behind_it() -> None:
    """R12.C4. A bare incremental number is unfalsifiable; the same number with its arms is not."""
    metrics = _metrics(
        incremental=IncrementalFinding(
            value=450_000,
            control_case_count=1_200,
            treatment_case_count=1_180,
            lift=Decimal("0.0620"),
            lift_ci_low=Decimal("0.0180"),
            lift_ci_high=Decimal("0.1060"),
        ),
        labels=("RECOVERY_GROSS_OF_REFUNDS",),
    )
    document = metrics_document(metrics, currency="INR")
    incremental = document["incremental_recovered_revenue"]
    assert isinstance(incremental, dict)
    assert incremental["status"] == "ESTABLISHED"
    assert incremental["amount"]["formatted"] == "₹4,500.00"
    assert incremental["control_case_count"] == 1_200
    assert incremental["lift_interval"] == "[0.0180, 0.1060]"
    assert document["causality_established"] is True


def test_the_degraded_metrics_shape_names_the_figure_and_keeps_the_caveat() -> None:
    """R14.C16, deterministically. Both halves, and the second is the one easy to lose.

    The incremental field becomes a data-unavailable marker naming itself, **and** the report still
    carries ``CAUSALITY_NOT_ESTABLISHED``. A figure that could not be computed is certainly not a
    causal claim that was established, so degrading has to fail toward the safe statement rather
    than dropping the caveat along with the number.
    """
    metrics = _metrics(
        incremental=IncrementalFinding(
            value=NOT_ESTABLISHED, refusal_codes=("METRICS_TIMEOUT",)
        ),
        labels=("RECOVERY_GROSS_OF_REFUNDS", "CAUSALITY_NOT_ESTABLISHED"),
    )
    document = metrics_document(metrics, currency="INR", incremental_available=False)

    incremental = document["incremental_recovered_revenue"]
    assert isinstance(incremental, dict)
    assert incremental["status"] == DATA_UNAVAILABLE
    assert incremental["figure"] == "incremental_recovered_revenue"
    assert "DASHBOARD_METRICS_TIMEOUT" in str(incremental["detail"])

    # Everything else still returns, with its own timestamps.
    assert document["case_count"] == 40
    assert document["revenue_at_risk"]["formatted"] == "₹80,000.00"
    assert document["computed_at"] == "2026-02-01T12:00:00+00:00"
    assert "CAUSALITY_NOT_ESTABLISHED" in document["labels"]
    assert document["causality_established"] is False


def test_a_synthetic_report_is_labelled_and_says_so_in_a_boolean_too() -> None:
    """One synthetic case labels the whole report; the flag exists so a client cannot miss it."""
    metrics = _metrics(
        incremental=IncrementalFinding(value=NOT_ESTABLISHED),
        labels=("RECOVERY_GROSS_OF_REFUNDS", "SYNTHETIC"),
    )
    document = metrics_document(metrics, currency="INR")
    assert document["is_synthetic"] is True
    assert "SYNTHETIC" in document["labels"]


# ---------------------------------------------------------------------------
# Constants that must not drift from the domain
# ---------------------------------------------------------------------------


def test_the_refusal_reasons_are_exactly_the_two_null_selection_reasons() -> None:
    """A third reason added to the domain must be given words rather than rendered blank."""
    assert {
        SelectionReason.NO_POSITIVE_VALUE.value,
        SelectionReason.HIGH_BASELINE_NO_INTERVENTION.value,
    } == NULL_SELECTION_REASONS
    assert set(SelectionReason) - {SelectionReason.HIGHEST_NET_VALUE} == {
        SelectionReason.NO_POSITIVE_VALUE,
        SelectionReason.HIGH_BASELINE_NO_INTERVENTION,
    }, "a new selection reason needs a refusal explanation or an explicit decision not to have one"


def test_the_views_executable_set_matches_the_domain() -> None:
    """``is_executable`` on a candidate must mean what ``domain.actions`` means by it.

    The set is restated in ``api.views`` so a reader can see what the flag claims without following
    an import. Restating it is only safe if something checks the two agree, which is this.
    """
    from revora.api import views

    assert views._EXECUTABLE == EXECUTABLE_ACTIONS