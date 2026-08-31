"""Tests that prove the schema rejects what it must.

Every test in here needs a real PostgreSQL, because every claim in here is about
PostgreSQL behaviour: partial unique indexes, ``ON CONFLICT``, triggers, row-level
security and constraint evaluation under concurrency. A fake or SQLite would agree
with the assertions and prove nothing.
"""
