"""Wiring one whole Revora up so the Demonstration_Loader can drive it.

``revora.synthetic.demo`` speaks HTTP and asks for a worker; it deliberately builds neither.
This module supplies both for the ``harness`` and ``pg`` tiers: an in-process application reached
through Starlette's test transport, the real worker registry with the scriptable Razorpay fake
substituted, the real ticker, and a :class:`~revora.platform.clock.ManualClock` the loader can
move.

**Nothing here is a shortcut around a path.** The transport carries raw bytes to the real
application, so a signature the loader computed wrongly fails exactly as it would over a socket.
The worker is ``build_registry`` with two arguments changed: the provider client, which is the
fake, and the provenance, which is the one seam by which a generated case gets labelled
``SYNTHETIC`` on its row (see ``revora.detection.service.run_detection``). The ticker is
``revora.jobs.ticker.tick``. There is no fourth thing.

**Why the fake provider rather than Razorpay test mode.** No gated test may make a real network
call, and the ``harness`` tier is gated even though it is nightly. The consequence is stated
plainly rather than hidden: the authoritative reads in a harness run are genuine ``fetch_payment``
calls against a fake, so ``observed_recovered_revenue`` from a harness run is money that moved in
a *simulated* provider. R28.C2's three Verified_Demo_Recoveries — money that moved in Razorpay
test mode — are a documented manual step in ``RUNBOOK.md``, and
``revora.synthetic.demo.verified_test_mode_capability`` returns ``False`` here so the batch
reports zero of them rather than counting a scripted capture as one.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from revora.api.app import create_app
from revora.api.auth import DASHBOARD_KEY_HEADER
from revora.domain.enums import Provenance
from revora.jobs.ticker import tick
from revora.jobs.worker import build_registry, run_once
from revora.persistence.repositories.engine import build_engine, dispose_engine, set_engine
from revora.platform import crypto
from revora.platform.clock import ManualClock, using_clock
from revora.platform.secrets import SecretStore, set_secret_store
from revora.synthetic.demo import DemoTenant, HttpResult
from tests.fakes.razorpay import FakeRazorpay, ProviderBehaviour, as_provider_client
from tests.pg_support import insert_merchant

__all__ = [
    "DEMO_DASHBOARD_KEY",
    "DEMO_WEBHOOK_SECRET",
    "MAX_DRAIN_PASSES",
    "DemoHarness",
    "InProcessTransport",
    "InProcessWorker",
    "demo_harness",
]

DEMO_DASHBOARD_KEY = "demo-batch-operator-key"
DEMO_WEBHOOK_SECRET = "demo-batch-webhook-secret"

MAX_DRAIN_PASSES = 40
"""A bound rather than ``while True``.

A pipeline that fails to advance fails the run instead of hanging it. Higher than the
integration tier's twenty-four because a batch drains a thousand cases' worth of chained jobs in
one pass and a legitimate optimistic-concurrency loss costs a pass; ``run_once`` returning zero
ends the loop long before the bound on any healthy run."""


class _Resolver:
    """Answers the per-merchant prefixed credentials for any slug, plus the fixed keys.

    The same shape the integration tier uses. Prefix-matched rather than enumerated because the
    dashboard key and the webhook secret are per merchant and a batch creates its merchant at
    run time, so there is no slug to enumerate when the store is built.
    """

    def __init__(self, values: Mapping[str, str]) -> None:
        self._values = dict(values)

    def get(self, name: str) -> str | None:
        if name.startswith("REVORA_DASHBOARD_KEYS_"):
            return DEMO_DASHBOARD_KEY
        if name.startswith("REVORA_WEBHOOK_SECRETS_"):
            return DEMO_WEBHOOK_SECRET
        return self._values.get(name)


def _secret_store() -> SecretStore:
    """Every credential the batch needs, and deliberately no LLM one.

    ``REVORA_CUSTOMER_TOKEN_SIGNING_SECRETS`` is the one whose absence would be silent and fatal:
    without it ``execute_approved_action`` refuses with ``TOKEN_ISSUE_FAILED /
    CREDENTIAL_UNAVAILABLE`` and **no action ever executes**, so a batch would produce a thousand
    cases, zero payment links and zero recovered revenue while every individual component looked
    healthy.
    """
    return SecretStore(
        _Resolver(
            {
                "REVORA_PAYLOAD_ENCRYPTION_KEYS": "1:"
                + base64.b64encode(b"D" * 32).decode(),
                "REVORA_CUSTOMER_KEY_SECRET": base64.b64encode(b"E" * 32).decode(),
                "REVORA_CUSTOMER_TOKEN_SIGNING_SECRETS": "1:"
                + base64.b64encode(b"F" * 32).decode(),
                "REVORA_SESSION_TOKEN_SECRET": base64.b64encode(b"G" * 32).decode(),
                "REVORA_RAZORPAY_KEY_ID": "rzp_test_demo_batch",
                "REVORA_RAZORPAY_KEY_SECRET": "demo-batch-secret",
            }
        )
    )


class InProcessTransport:
    """:class:`~revora.synthetic.demo.DemoTransport` over an in-process application.

    Raw bytes in, status and decoded body out. A body that is not JSON becomes an empty mapping
    rather than an error, because several refusals on the customer surface answer with no body at
    all and the loader's only question about those is the status code.
    """

    __slots__ = ("_client",)

    def __init__(self, client: TestClient) -> None:
        self._client = client

    def request(
        self,
        method: str,
        path: str,
        *,
        content: bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResult:
        response = self._client.request(
            method, path, content=content, headers=dict(headers or {})
        )
        try:
            body = response.json()
        except (ValueError, json.JSONDecodeError):
            body = {}
        return HttpResult(
            status_code=response.status_code,
            body=body if isinstance(body, dict) else {"body": body},
        )


class InProcessWorker:
    """:class:`~revora.synthetic.demo.DemoWorker` over the real worker and the real ticker.

    The registry is rebuilt on every drain rather than captured once, so a payment scripted on
    the fake between drains takes effect — which is how the loader stages "the read says failed,
    then the capture arrives".

    ``provenance`` and ``synthetic_run_id`` are passed to ``build_registry`` together, and it
    refuses one without the other. That is the whole mechanism by which R28.C1's ``SYNTHETIC``
    label reaches ``recovery_case`` without the loader writing a row.
    """

    __slots__ = ("_fake", "_provenance", "_synthetic_run_id", "_worker_id")

    def __init__(
        self,
        fake: FakeRazorpay,
        *,
        synthetic_run_id: uuid.UUID,
        worker_id: str = "demo-batch-worker",
    ) -> None:
        self._fake = fake
        self._provenance = Provenance.SYNTHETIC
        self._synthetic_run_id = synthetic_run_id
        self._worker_id = worker_id

    def _registry(self) -> dict[str, object]:
        return build_registry(  # type: ignore[return-value]
            provider=as_provider_client(self._fake),
            provenance=self._provenance,
            synthetic_run_id=self._synthetic_run_id,
        )

    def drain(self) -> int:
        handlers = self._registry()
        for used in range(1, MAX_DRAIN_PASSES + 1):
            if run_once(self._worker_id, registry=handlers) == 0:  # type: ignore[arg-type]
                return used
        return MAX_DRAIN_PASSES

    def tick(self, kinds: Sequence[str]) -> int:
        return tick(kinds=tuple(kinds)).enqueued


@dataclass(frozen=True, slots=True)
class DemoHarness:
    """Everything a batch run needs, assembled.

    ``script_payment`` is handed to the loader as its provider oracle: told a payment id, an
    amount and whether the money arrived, it pins what an authoritative read will report for
    *that* payment. Per payment rather than process-wide, because a read whose amount differs
    from the case's is a partial capture to the Outcome_Monitor and a batch has a thousand
    different amounts.
    """

    tenant: DemoTenant
    transport: InProcessTransport
    worker: InProcessWorker
    fake: FakeRazorpay
    clock: ManualClock
    engine: Engine
    synthetic_run_id: uuid.UUID

    def advance(self, delta: timedelta) -> None:
        self.clock.advance(delta)

    def script_payment(self, payment_id: str, amount: int, captured: bool) -> None:
        from revora.domain.payment_event import PaymentStatus

        self.fake.script_payment(
            payment_id,
            amount=amount,
            statuses=(PaymentStatus.CAPTURED if captured else PaymentStatus.FAILED,),
        )


def demo_harness(engine: Engine) -> Iterator[DemoHarness]:
    """Build a merchant, an application, a worker and a frozen clock for one batch run.

    A generator rather than a fixture so both the ``harness`` tier and the ``pg`` tier can use it
    with different case counts, and so the clock substitution is scoped to a ``with`` block that
    restores the real one even when a run fails part way.

    **The ``synthetic_run_id`` is minted here, before the loader runs.** It has to be: the worker
    registry carries it, and the registry exists before the first webhook. The loader writes the
    ``synthetic_run`` row itself under this id, which keeps the row's ground truth and the cases
    that point at it consistent by construction.
    """
    previous_secrets = set_secret_store(_secret_store())
    crypto.reset_cached_material()
    # `str(url)` masks the password, and an engine built from a masked URL fails to
    # authenticate with a message about the *user* rather than about the masking. Rendered
    # explicitly so the failure cannot happen at all.
    set_engine(build_engine(engine.url.render_as_string(hide_password=False)))
    clock = ManualClock()
    try:
        with using_clock(clock):
            app = create_app(verify_schema=False, serve_dashboard=False)
            with TestClient(app) as client:
                merchant_id = insert_merchant(engine, display_name="Demonstration batch")
                with engine.begin() as connection:
                    slug = str(
                        connection.execute(
                            text("SELECT slug FROM merchant WHERE id = :m"),
                            {"m": str(merchant_id)},
                        ).scalar_one()
                    )
                    connection.execute(
                        text(
                            """
                            INSERT INTO merchant_user (
                                id, merchant_id, email_masked, email_key, role, is_active,
                                created_at
                            ) VALUES (
                                :id, :m, '****demo@example.invalid', :key, 'operator', true,
                                now()
                            )
                            """
                        ),
                        {
                            "id": str(uuid.uuid4()),
                            "m": str(merchant_id),
                            "key": f"emailkey-demo-{merchant_id}",
                        },
                    )

                session = client.post(
                    "/auth/sessions",
                    json={"merchant_slug": slug},
                    headers={DASHBOARD_KEY_HEADER: DEMO_DASHBOARD_KEY},
                )
                assert session.status_code == 201, session.text
                token = session.json()["token"]

                fake = FakeRazorpay(ProviderBehaviour())
                synthetic_run_id = uuid.uuid4()
                yield DemoHarness(
                    tenant=DemoTenant(
                        merchant_id=merchant_id,
                        slug=slug,
                        webhook_secret=DEMO_WEBHOOK_SECRET,
                        dashboard_headers={"Authorization": f"Bearer {token}"},
                    ),
                    transport=InProcessTransport(client),
                    worker=InProcessWorker(fake, synthetic_run_id=synthetic_run_id),
                    fake=fake,
                    clock=clock,
                    engine=engine,
                    synthetic_run_id=synthetic_run_id,
                )
    finally:
        dispose_engine()
        set_secret_store(previous_secrets)
        crypto.reset_cached_material()
