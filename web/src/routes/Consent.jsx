/**
 * Recording an opt-out (R17.C10).
 *
 * The screen exists because an opt-out is the one thing in this system an operator records on behalf
 * of a person who asked, and the reach of it is not obvious: **consent is keyed on the customer, not
 * on the payment**, so recording one here suppresses contact on cases that do not exist yet as well
 * as ones already open. The response reports how many open cases it just affected, which is the
 * number an operator actually wants to see.
 *
 * The contact is sent, not a derived key. The server derives `customer_key` with the same keyed hash
 * the ingestion path uses — that is what makes an opt-out recorded before a failure able to govern it
 * — and doing the derivation here would require this bundle to hold the secret.
 */

import { useState } from 'react'

import { useRecordConsent } from '../api/queries'
import { Panel } from '../components/Chrome'

export function Consent() {
  const [contact, setContact] = useState('')
  const [source, setSource] = useState('')
  const [optedOut, setOptedOut] = useState(true)
  const record = useRecordConsent()

  function submit(event) {
    event.preventDefault()
    record.mutate({ contact: contact.trim(), optedOut, source: source.trim() })
  }

  return (
    <Panel
      title="Customer contact preference"
      subtitle="Keyed on the customer, so it governs cases that do not exist yet"
    >
      <form className="form" onSubmit={submit}>
        <label className="field">
          <span className="field__label">Contact</span>
          <span className="field__hint">
            Phone or email, as the customer gave it. Stored only as a keyed hash and a masked form —
            the raw value is never persisted.
          </span>
          <input
            className="field__input"
            value={contact}
            onChange={(event) => {
              setContact(event.target.value)
            }}
            required
          />
        </label>

        <label className="field">
          <span className="field__label">Source</span>
          <span className="field__hint">
            Where the request came from — a ticket id, a call reference. Recorded on the audit trail,
            because &ldquo;who asked for this?&rdquo; is a question that gets asked later.
          </span>
          <input
            className="field__input"
            value={source}
            onChange={(event) => {
              setSource(event.target.value)
            }}
            required
          />
        </label>

        <fieldset className="field">
          <legend className="field__label">Preference</legend>
          <label className="radio">
            <input
              type="radio"
              name="preference"
              checked={optedOut}
              onChange={() => {
                setOptedOut(true)
              }}
            />
            <span>
              <strong>Opted out.</strong> Revora will not contact this customer about any failed
              payment, on this case or any future one.
            </span>
          </label>
          <label className="radio">
            <input
              type="radio"
              name="preference"
              checked={!optedOut}
              onChange={() => {
                setOptedOut(false)
              }}
            />
            <span>
              <strong>Consent on record.</strong> Contact is permitted, subject to every other policy
              check.
            </span>
          </label>
        </fieldset>

        <button type="submit" className="button button--primary" disabled={record.isPending}>
          {record.isPending ? 'Recording…' : 'Record preference'}
        </button>
      </form>

      {record.isError && (
        <p className="status status--error" role="alert">
          {record.error.message}
        </p>
      )}

      {record.isSuccess && (
        <div className="notice" role="status">
          <p>
            <strong>Recorded.</strong> {record.data.detail}
          </p>
          <p>
            {record.data.affected_open_case_count} open case
            {record.data.affected_open_case_count === 1 ? '' : 's'} affected. Future cases for this
            customer are governed too — that cross-case reach is the whole point of keying consent on
            the person rather than the payment.
          </p>
          {record.data.supersedes_consent_id !== null && (
            <p>
              This supersedes an earlier record. The earlier one is retained; preferences are a
              history, not a mutable flag.
            </p>
          )}
        </div>
      )}
    </Panel>
  )
}
