"""Append-only audit enforcement: a revoked grant and a trigger, plus the reject log.

Two independent mechanisms, deliberately redundant.

**The revoked grant** is the primary control. ``revora_app`` has ``SELECT`` and
``INSERT`` on ``audit_record`` and nothing else, so an ``UPDATE`` never reaches the
row.

**The trigger** is the backstop. A grant can be restored by a careless migration, by
an operator debugging something at 2am, or by a ``GRANT ALL`` in a helper script. The
trigger cannot be re-enabled by accident in the same way, and it raises with
``insufficient_privilege`` so a caller sees the same class of error either way.

**The reject log** is a third function, insert-only and ``SECURITY DEFINER``, because
R11.C9 requires a rejected mutation attempt to be recorded naming the requesting
actor. It has to be a separate function: by the time the trigger has fired, the
transaction that attempted the mutation is aborted and cannot write anything. The
caller opens a fresh transaction and calls this.

**No hash chain, deliberately.** A chain over records would detect out-of-band
tampering by someone with direct database access — a party who can also rewrite the
chain. It would be reassurance rather than protection. If tamper-evidence against a
privileged insider becomes a requirement, the answer is shipping records to
append-only external storage.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "revora_app"
"""The role the application connects as. Named in the design. The migration runs as
the owner, which is a different role — that separation is what makes a revoked grant
mean anything."""


_AUDIT_IMMUTABLE_FUNCTION = """
CREATE OR REPLACE FUNCTION audit_immutable() RETURNS trigger
LANGUAGE plpgsql AS $function$
BEGIN
  -- 42501 is insufficient_privilege: the same class the revoked grant produces, so
  -- a caller cannot tell which of the two mechanisms stopped it and does not need to.
  RAISE EXCEPTION 'audit_record is append-only (attempted %)', TG_OP
    USING ERRCODE = '42501';
END
$function$;
"""

_AUDIT_TRIGGER = """
CREATE TRIGGER audit_no_mutation
  BEFORE UPDATE OR DELETE ON audit_record
  FOR EACH ROW EXECUTE FUNCTION audit_immutable();
"""

_REJECT_FUNCTION = """
CREATE OR REPLACE FUNCTION record_audit_mutation_rejected(
  p_merchant_id uuid,
  p_actor text,
  p_operation text,
  p_correlation_id uuid
) RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $function$
DECLARE
  new_id uuid;
BEGIN
  -- Insert-only, and SECURITY DEFINER so it keeps working even if the application
  -- role's grants are tightened further. case_id and seq are both NULL: this record
  -- is about an attempted mutation, not about a case, and the
  -- case_and_seq_together check keeps the pair honest.
  --
  -- The attempted operation goes in `evidence`, not in `action`: `action` is
  -- constrained to the CandidateAction enum, and 'UPDATE' is not a recovery action.
  INSERT INTO audit_record (
    merchant_id, case_id, seq, event_type, actor, evidence, correlation_id, occurred_at
  ) VALUES (
    p_merchant_id, NULL, NULL, 'AUDIT_MUTATION_REJECTED', p_actor,
    jsonb_build_object('attempted_operation', p_operation),
    p_correlation_id, now()
  )
  RETURNING id INTO new_id;
  RETURN new_id;
END
$function$;
"""


def _grant_block(statements: str) -> str:
    """Wrap privilege changes so they are skipped when the app role does not exist.

    A developer database created by ``initdb`` has no ``revora_app``. Failing the
    migration there would mean the append-only trigger — the part that does not
    depend on the role at all — never gets installed locally, and the behaviour
    developers test against would differ from production in exactly the wrong
    direction.
    """
    return f"""
DO $do$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
    {statements}
  END IF;
END
$do$;
"""


def upgrade() -> None:
    op.execute(_AUDIT_IMMUTABLE_FUNCTION)
    op.execute(_AUDIT_TRIGGER)
    op.execute(_REJECT_FUNCTION)

    # Mechanism 1: the application role can read and append, and nothing else.
    op.execute(
        _grant_block(
            f"""
    REVOKE UPDATE, DELETE, TRUNCATE ON audit_record FROM {APP_ROLE};
    GRANT SELECT, INSERT ON audit_record TO {APP_ROLE};
    GRANT EXECUTE ON FUNCTION record_audit_mutation_rejected(uuid, text, text, uuid)
      TO {APP_ROLE};
    """
        )
    )

    op.execute(
        "COMMENT ON TRIGGER audit_no_mutation ON audit_record IS "
        "'Second of two append-only mechanisms. The first is the revoked UPDATE/DELETE/"
        "TRUNCATE grant; this catches a grant restored by accident.'"
    )
    op.execute(
        "COMMENT ON FUNCTION record_audit_mutation_rejected(uuid, text, text, uuid) IS "
        "'Records AUDIT_MUTATION_REJECTED naming the actor (R11.C9). Must be called on a "
        "fresh transaction: the one that attempted the mutation is already aborted.'"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_no_mutation ON audit_record")
    op.execute("DROP FUNCTION IF EXISTS audit_immutable()")
    op.execute(
        "DROP FUNCTION IF EXISTS record_audit_mutation_rejected(uuid, text, text, uuid)"
    )
    # The grant is deliberately not restored. Reversing a migration should not hand
    # back the ability to rewrite the audit log.
