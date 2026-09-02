# Hypothesis failure database

Committed counterexamples. **Do not delete files here to make a build pass.**

Hypothesis writes one small file per distinct property failure, holding the shrunk input that
reproduced it. On the next run it replays those inputs *first*, so a bug that has been fixed stays
fixed and a regression fails immediately rather than after however many examples it takes to
rediscover the input.

By default this database lives in `.hypothesis/examples`, which Hypothesis gitignores — meaning a
shrunk counterexample survives exactly as long as one developer's working copy. `tests/conftest.py`
points every profile here instead, because a property suite whose failures are not version
controlled cannot claim that anything stays fixed.

**A file appearing in a diff is a real signal.** It means a property found something on that
branch. A green suite adds nothing here.

The `.hypothesis/` directory is still used, and is still ignored: it caches the unicode tables and
scratch data, none of which has regression value.
