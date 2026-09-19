# Contributing

## Setup

There is nothing to install. `requirements.txt` lists no third-party
packages — this project is standard library only, Python 3.10+. Running
`pip install -r requirements.txt` works but installs nothing; it is there so
the same command works in any CI or Docker recipe that expects the file.

Use a virtual environment scoped to this repository only. Every repository
gets its own virtual environment — never reuse one venv across repos, even
for a project like this one with no third-party dependencies. A shared venv
hides a missing dependency until a fresh environment, such as CI, hits it
first.

```
python -m venv .venv
.venv\Scripts\activate       # Windows
source .venv/bin/activate    # macOS/Linux
```

## Running the tests

This repository has no `tests/` directory. The self-test built into
`reconcile.py` is the test suite:

```
python -m compileall -q .
python reconcile.py --selftest
```

This is exactly what CI runs, on Python 3.10 and 3.12.

## Adding a check or fixing a bug

A new check starts with a failing test in this repository's terms:

1. Add a case to the self-test block in `reconcile.py` (the `selftest()`
   function, using its `check()` helper) that captures the bug or the new
   behavior you want, and confirm it fails with
   `python reconcile.py --selftest`.
2. Implement the fix.
3. Confirm `python reconcile.py --selftest` passes, with your new check
   included in the count it prints.

## Commit style

Short, lowercase, descriptive sentences. Occasionally `type: description`.
Real examples from this repository's history:

```
invoice-reconciler: match a bank statement against open invoices, five passes, no dependencies
reconcile: detect the date convention per file, drop shortfalls that a later payment covered, add a US-format sample
Run the tests in CI on every push
```

## Pull requests

- Keep changes scoped to one thing.
- CI must pass: `python -m compileall -q .` and `python reconcile.py --selftest`,
  on Python 3.10 and 3.12.
- Do not commit real bank or invoice data, or anything with live account
  numbers. Only the files under `samples/` are tracked — if you add a
  sample, make sure it is fabricated data.
