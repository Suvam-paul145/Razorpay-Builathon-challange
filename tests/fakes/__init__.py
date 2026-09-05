"""Scriptable stand-ins for the external services Revora depends on.

A fake here is not a convenience. Every guarantee the system claims is a statement
about what happens when an external service misbehaves, so the fakes are where those
guarantees are actually exercised — and all of them record every call, because
"no external call was made" is a claim about a negative and a negative needs a log.
"""
