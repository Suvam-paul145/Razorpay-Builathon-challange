"""P1, P29, P31, P32. Revora never acts without authority, without a record, or with a raw contact.

Four properties that are really one statement in four places: **an external effect is permitted only
when the system can account for it.** Each names a different way that accounting can fail.

* **P1 — authority.** Every external effect is preceded by exactly one unconsumed matching approval
  (R8.C4, R9.C5). Not "an approval existed" — *one*, *unconsumed*, and *matching the action*. Each
  qualifier removes a different way to act twice on one decision.

* **P29 — accountability.** If an audit record cannot be persisted, no further external action for
  that case may be issued (R11.C10). An effect nobody can later explain is worse than a delayed one,
  because the merchant is answerable for it and has nothing to answer with.

* **P31 — independence.** The deterministic diagnosis path needs no model, and an unavailable
  reasoning layer changes nothing structural (R3.C1, R4.C4). In this build the layer does not exist
  at all, which makes the property checkable in its strongest form.

* **P32 — disclosure.** No raw contact value reaches an audit record or a log line
  (R17.C8, R17.C15), and a field outside the outbound contract is blocked before transmission
  rather than after.

``pure`` and ``model`` where the claim is about a function, ``pg`` where it is about a transaction.
The tier is per-test, not per-file, because P1 is only true of a real unique constraint while P32 is
true of a pure masking function — and running the second against a database would slow it down
without strengthening it.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Iterator
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from revora.audit.writer import (
    AuditEntry,
    AuditWriter,
    block_case,
    clear_case_block,
    is_case_blocked,
)
from revora.domain.actions import CandidateAction
from revora.domain.failure_taxonomy import REASON_TO_CAUSE
from revora.platform import crypto
from revora.platform.masking import (
    MASK_CHARACTER,
    MASK_DISCLOSURE_LENGTH,
    FieldKind,
    mask_record,
    mask_value,
)
from revora.platform.secrets import SecretStore, set_secret_store

# ---------------------------------------------------------------------------
# Generators for the values that must never leak
# ---------------------------------------------------------------------------

_PHONES = st.from_regex(r"\+91[6-9][0-9]{9}", fullmatch=True)
_EMAILS = st.from_regex(r"[a-z]{3,12}@[a-z]{3,10}\.(com|invalid|in)", fullmatch=True)
contacts = st.one_of(_PHONES, _EMAILS)
"""Phone numbers and email addresses, in the shapes Razorpay actually sends.

Not arbitrary text: the masking rules are keyed on *field names* rather than on value shapes, so the
interesting question is whether a value that looks like a contact survives a field that was not
recognised as one. Generating realistic contacts is what makes that question askable."""


class _Resolver:
    def get(self, name: str) -> str | None:
        if name == "REVORA_CUSTOMER_KEY_SECRET":
            return base64.b64encode(b"D" * 32).decode()
        return None


@pytest.fixture(scope="module", autouse=True)
def customer_key_secret() -> Iterator[None]:
    """The key-derivation secret, installed once for the module.

    Module-scoped and autouse rather than function-scoped, because Hypothesis rightly refuses a
    function-scoped fixture under ``@given``: the fixture is *not* reset between generated examples,
    so a test that depended on fresh setup per example would be silently wrong. Here there is
    nothing to reset — installing a secret is idempotent and the derivation is a pure function of it
    — so module scope is both correct and the only scope that does not trip the health check.
    """
    previous = set_secret_store(SecretStore(_Resolver()))
    crypto.reset_cached_material()
    try:
        yield
    finally:
        set_secret_store(previous)
        crypto.reset_cached_material()


# ---------------------------------------------------------------------------
# P32: no contact value reaches an audit record or a log line
# ---------------------------------------------------------------------------


@pytest.mark.pure
@settings(max_examples=500)
@given(contact=contacts)
def test_a_masked_contact_never_contains_the_whole_value(contact: str) -> None:
    """R17.C8. The mask may disclose a short suffix and nothing more.

    The suffix exists so a support agent can confirm they are talking about the right person, which
    is a real operational need — so the property is not "no digits appear", it is that the *whole*
    value never appears and that no more than the configured number of characters is disclosed.
    Stating it that way is what keeps the assertion honest as the disclosure length is tuned.
    """
    # `FieldKind.CONTACT`, not the string "contact". `mask_value` takes the enum, and passing a
    # string means it matches no kind and the value is returned untouched — which is exactly how a
    # masking call can silently do nothing.
    masked = mask_value(contact, FieldKind.CONTACT)
    assert isinstance(masked, str)

    assert contact not in masked, f"the whole contact survived masking: {masked}"
    assert MASK_CHARACTER in masked, f"nothing was masked: {masked}"

    # At most `MASK_DISCLOSURE_LENGTH` characters of the original tail are shown. Checked by
    # counting how long a shared suffix the two strings have, because that is precisely what
    # "disclosure" means here.
    shared = 0
    for original, shown in zip(reversed(contact), reversed(masked), strict=False):
        if original != shown:
            break
        shared += 1
    assert shared <= MASK_DISCLOSURE_LENGTH, (
        f"masking disclosed {shared} trailing characters of {contact!r}, above the "
        f"{MASK_DISCLOSURE_LENGTH} permitted: {masked}"
    )


@pytest.mark.pure
@settings(max_examples=500)
@given(contact=contacts, other=st.text(min_size=0, max_size=20))
def test_masking_a_record_removes_contacts_from_every_recognised_field(
    contact: str, other: str
) -> None:
    """R17.C8 over a whole nested document, which is the shape an audit field actually is.

    ``evidence`` and ``decision`` are JSONB and hold whatever the component recording them thought
    was relevant, so the masking walk has to descend. A rule that only handled the top level would
    pass every flat test and leak the first time somebody nested a payload under a key.
    """
    record = {
        "contact": contact,
        "email": contact,
        "nested": {"customer_contact": contact, "note": other},
        "list": [{"phone": contact}, other],
    }
    masked = mask_record(record)
    rendered = json.dumps(masked)
    assert contact not in rendered, f"a contact survived the masking walk: {rendered}"


@pytest.mark.pure
@settings(max_examples=200)
@given(contact=contacts)
def test_a_derived_customer_key_is_not_the_contact(
    contact: str, customer_key_secret: None
) -> None:
    """R17.C15. The key that consent is looked up by must not itself disclose the person.

    A keyed HMAC rather than a plain hash, so holding the table does not let an attacker
    confirm a guessed phone number by hashing it. That property cannot be tested directly here —
    it is a claim about the absence of the secret — but the necessary condition can be: the key is
    not the contact, is not a prefix of it, and is stable.
    """
    key = crypto.customer_key(contact)
    assert key != contact
    assert contact not in key
    assert key == crypto.customer_key(contact), "the derivation must be deterministic"


@pytest.mark.pure
@settings(max_examples=200)
@given(first=_PHONES, second=_PHONES)
def test_two_different_subscriber_numbers_derive_two_keys(first: str, second: str) -> None:
    """A collision would silently apply one person's opt-out to another's cases.

    Which is a failure in both directions at once: one customer keeps being contacted after asking
    not to be, and another stops being contacted without having asked.

    **Compared on the last ten digits, not on the raw strings, and that is the documented design
    rather than a concession.** ``customer_key`` normalises a phone number by stripping non-digits
    and keeping the subscriber portion, so ``+919876543210`` and ``919876543210`` and
    ``+91 98765-43210`` are one person — which is the whole point, since a provider sends the same
    number in several spellings. The first version of this test appended ``"x"`` to a number and
    expected a different key; that was the test misunderstanding the normalisation, not a collision.
    ``crypto`` marks the ten-digit rule an ``[ASSUMPTION]`` for INR merchants and notes that keys
    are only ever joined within one merchant, so a genuine cross-country collision is confined and
    fails toward a suppressed message rather than an unwanted one.
    """
    same_subscriber = first[-10:] == second[-10:]
    keys_match = crypto.customer_key(first) == crypto.customer_key(second)
    assert keys_match == same_subscriber, (
        f"{first} and {second} share a subscriber number: {same_subscriber}, but their keys match: "
        f"{keys_match}. A key must identify exactly the people the normalisation says it does."
    )


# ---------------------------------------------------------------------------
# P29: a failed audit write blocks further external action
# ---------------------------------------------------------------------------


@pytest.mark.pure
def test_a_blocked_case_reports_blocked_until_cleared() -> None:
    """R11.C10's mechanism, in isolation.

    The block is process-local and that is sufficient by design: the execution engine that consults
    it runs in the same worker, and a restart re-derives the block from the ``ATTEMPTED`` intents
    reconciliation finds. Asserted here so the three functions cannot drift into disagreeing —
    ``block_case`` writing one key and ``is_case_blocked`` reading another would be invisible until
    an audit failure actually happened, which is the worst possible time to discover it.
    """
    merchant_id, case_id = uuid.uuid4(), uuid.uuid4()
    other_case = uuid.uuid4()

    assert not is_case_blocked(merchant_id, case_id)
    block_case(merchant_id, case_id)
    try:
        assert is_case_blocked(merchant_id, case_id)
        # Scoped to the case, not the merchant. A single audit failure must not stop recovery for
        # every other case belonging to that merchant — that would turn a narrow fault into an
        # outage, and R11.C10 asks for the narrow behaviour.
        assert not is_case_blocked(merchant_id, other_case)
        # And scoped to the tenant.
        assert not is_case_blocked(uuid.uuid4(), case_id)
    finally:
        clear_case_block(merchant_id, case_id)
    assert not is_case_blocked(merchant_id, case_id)


@pytest.mark.pure
def test_the_execution_engine_consults_the_block_before_anything_else() -> None:
    """Structural. The check has to come before the provider call, and it does.

    Read out of the source rather than exercised, because staging a real audit failure mid-execution
    needs the crash harness and a database, and the claim here is narrower: that the engine consults
    ``is_case_blocked`` in the phase that *reserves*, which runs before the phase that calls out.

    Asserted per function rather than by position in the file. A file-wide text index was the first
    attempt and it was simply wrong: ``_reserve_under_lock`` is *defined* below the line that issues
    the provider call, even though it *runs* before it. Comparing offsets in the source measured
    layout, not order.
    """
    import inspect

    from revora.execution import engine

    reserve = inspect.getsource(engine._reserve_under_lock)
    execute = inspect.getsource(engine.execute_approved_action)

    assert "is_case_blocked" in reserve, (
        "the reservation phase does not consult the audit block; an effect that cannot be audited "
        "must be withheld, not explained afterwards"
    )
    assert "AUDIT_BLOCKED" in reserve

    # The reservation phase itself must not be able to reach the provider — that is what makes
    # "checked before the call" true regardless of how the two phases are ordered in the file.
    # Both provider methods, because R24 gave the engine a second call to make: a promise
    # follow-up against a live link goes out through ``notify_by``, and a reservation phase that
    # could reach *that* one would be issuing an unauditable message rather than an unauditable
    # link — which is worse, because a resend cannot be read back to find out whether it happened.
    for method in ("create_payment_link", "notify_by"):
        assert method not in reserve, (
            f"the reservation phase can reach {method}, so the audit block no longer precedes the "
            "external effect"
        )

    # And the caller reserves before it calls out, on both branches.
    assert execute.index("_reserve_under_lock") < execute.index("create_payment_link")
    assert execute.index("_reserve_under_lock") < execute.index("notify_by")


# ---------------------------------------------------------------------------
# P31: the deterministic path needs no model
# ---------------------------------------------------------------------------


@pytest.mark.pure
def test_the_reasoning_package_re_exports_nothing() -> None:
    """P31's structural half, restated now that there *is* an adapter.

    This test used to assert that ``revora.reasoning`` had no public surface at all, on the
    grounds that task 14 was dropped and the package was empty. Task 49.2 changed that
    deliberately, which is what the previous version of this docstring said would have to
    happen. The two-run distribution comparison the design originally specified now belongs to
    Properties 49 and 51 in task 49.5, where a run with the credential absent is compared
    against a run with every response rejected.

    What remains here is the narrower claim, and it is still worth holding: the package's
    ``__init__`` re-exports nothing. Every consumer therefore names the submodule it depends on
    — ``revora.reasoning.adapter``, not ``revora.reasoning`` — so a grep for the adapter finds
    every component that can reach one, and a convenience re-export cannot quietly turn "the
    pipeline holds an adapter" into "anything that imports the package holds one".

    Read from the source rather than through ``dir()``, because importing any submodule binds it
    as an attribute of the package and would make this assertion depend on test ordering.
    """
    from pathlib import Path

    init = Path(__file__).resolve().parents[2] / "revora" / "reasoning" / "__init__.py"
    statements = [
        line
        for line in init.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert statements == [], (
        f"revora/reasoning/__init__.py has gained content {statements}; a re-export makes the "
        "set of components that can reach a model unreadable from their imports"
    )


SANCTIONED_REASONING_CALLERS: frozenset[str] = frozenset({"jobs/pipeline.py"})
"""The complete set of modules permitted to import the reasoning layer.

One module. Task 49.3 put all three invocation sites in ``revora/jobs/pipeline.py``, and this
set is the assertion that a fourth caller cannot appear without somebody editing this line.
Adding a name here is the review step: it means a new component can now reach a model, and
the question "which components can" stops being answerable by reading one entry."""


@pytest.mark.pure
def test_only_the_job_pipeline_imports_the_reasoning_layer() -> None:
    """The decision-*making* components still cannot depend on a model being there.

    **This test changed in task 49.3 and the change is a narrowing, not a relaxation.** It used
    to assert that *no* module imported ``revora.reasoning`` at all, which was the right claim
    while the package was declared and unwired: a stray import would have turned an empty
    package into an ``ImportError`` at startup. The package now has three invocation sites, so
    that form of the claim is no longer available — and asserting it would mean deleting the
    wiring rather than checking it.

    What replaces it is the stronger, more specific statement. ``revora.jobs.pipeline`` is the
    single sanctioned caller, named in :data:`SANCTIONED_REASONING_CALLERS`, and every other
    module in ``revora`` is still unable to import the layer. That keeps the property the
    original test existed for — no component that *decides* anything can reach a model — while
    making the one component that may reach one enumerable rather than implicit.

    ``import-linter`` carries the half a test cannot: ``policy-isolation`` and
    ``optimizer-isolation`` forbid two named packages, and the ``layering`` band makes
    ``revora.reasoning`` a sibling of ``revora.diagnosis``, ``revora.estimation`` and
    ``revora.memory``, so none of the four can import each other. Those contracts are the
    authority. This test adds what a contract cannot express: that the set of callers *above*
    that band is exactly one, so ``revora.api``, ``revora.execution``, ``revora.outcome``,
    ``revora.metrics``, ``revora.experiment`` and ``revora.timeline`` — every one of which the
    layering would permit to import reasoning — do not.

    ``revora.execution`` is the interesting member of that list. It composes the customer-visible
    description, so it is the module a reader would expect to hold the adapter; it does not, and
    receives the validated draft as ``execute_approved_action``'s ``ai_description`` argument
    instead. That is what makes "the engine cannot tell a model's sentence from a template" true
    of the signature rather than of the current code.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "revora"
    # A docstring mentioning it is fine; an import is not.
    importing = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if "reasoning" not in path.parts
        and any(
            line.strip().startswith(("import revora.reasoning", "from revora.reasoning"))
            for line in path.read_text(encoding="utf-8").splitlines()
        )
    )
    unsanctioned = [name for name in importing if name not in SANCTIONED_REASONING_CALLERS]
    assert unsanctioned == [], (
        f"modules outside the sanctioned caller set import the reasoning layer: {unsanctioned}"
    )
    assert importing == sorted(SANCTIONED_REASONING_CALLERS), (
        "the sanctioned caller set no longer matches reality; a name listed there that imports "
        f"nothing makes the set read as permission nobody uses. Found {importing}"
    )


@pytest.mark.pure
@settings(max_examples=500)
@given(reason=st.sampled_from(sorted(REASON_TO_CAUSE)))
def test_every_verified_reason_classifies_without_a_model(reason: str) -> None:
    """R3.C1. The deterministic table decides, for every reason the provider documents.

    This is the positive half of P31: not merely that no model is called, but that none is
    *needed* — every verified reason resolves to a cause through arithmetic on a table. If this
    ever failed for some reason, the honest response would be to extend the table, and the failure
    names which entry to add.
    """
    from revora.domain.failure_taxonomy import classify_failure
    from revora.platform.config import default_configuration

    outcome = classify_failure(
        error_reason=reason,
        error_source=None,
        error_step=None,
        error_code=None,
        risk_reason_codes=default_configuration().RISK_REASON_CODES,
    )
    assert outcome.cause is not None, f"{reason} produced no cause from the deterministic table"
    assert outcome.matched, f"{reason} is in the verified table but did not match: {outcome}"


# ---------------------------------------------------------------------------
# P1: authority, as a statement about the recorded decision
# ---------------------------------------------------------------------------


@pytest.mark.pure
@settings(max_examples=300)
@given(
    action=st.sampled_from(sorted(CandidateAction, key=lambda a: a.value)),
    ordinal=st.integers(min_value=1, max_value=3),
)
def test_an_idempotency_key_is_a_function_of_the_action_and_the_attempt(
    action: CandidateAction, ordinal: int
) -> None:
    """The mechanism P1 rests on: one key per (case, action, attempt), derived not drawn.

    If the key were random then two executions of one approval would produce two keys, the unique
    constraint would not collide, and the provider would receive two creates. Determinism is what
    makes "exactly one unconsumed approval" enforceable by the database rather than by discipline.

    Stability across calls is asserted because the derivation is used at two different moments — the
    policy engine computes a *prospective* key to test for a duplicate, and the execution engine
    computes the real one. Those two must agree or the duplicate check silently never fires.
    """
    from revora.domain.keys import execution_key

    case_id = uuid.uuid4()
    first = execution_key(case_id=case_id, action=action, attempt_ordinal=ordinal)
    second = execution_key(case_id=case_id, action=action, attempt_ordinal=ordinal)
    assert first == second

    assert first != execution_key(
        case_id=case_id, action=action, attempt_ordinal=ordinal + 1
    ), "a further attempt must be a different key, or a legitimate retry could never proceed"
    assert first != execution_key(
        case_id=uuid.uuid4(), action=action, attempt_ordinal=ordinal
    ), "two cases must not share a key, or one case's effect would suppress another's"
    assert len(first) <= 40, "the provider caps reference_id length"


@pytest.mark.pure
def test_an_audit_entry_cannot_carry_an_unrecognised_field() -> None:
    """R11.C8. The recorded field set is fixed and reviewable, not open ``**kwargs``.

    An open payload is how a component starts recording something nobody masked: the masking rules
    are keyed on field names, so a field that was never declared is a field with no rule. A frozen
    dataclass turns that into a ``TypeError`` at the call site.
    """
    with pytest.raises(TypeError):
        AuditEntry(  # type: ignore[call-arg]
            event_type="X", actor="test", customer_contact="+919000000000"
        )

    # And the declared set is what the writer knows how to handle.
    entry = AuditEntry(
        event_type="X",
        actor="test",
        confidence=Decimal("0.9"),
        evidence={"error_reason": "insufficient_funds"},
    )
    assert entry.confidence == Decimal("0.9")
    assert AuditWriter is not None
