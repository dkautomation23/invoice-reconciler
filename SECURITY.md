# Security Policy

## Supported Versions

There are no tagged releases yet. Only the latest commit on `main` is
supported. If you hit an issue, update to the latest commit on `main` before
reporting it.

## Reporting a Vulnerability

Report privately using GitHub's Private Vulnerability Reporting: open this
repository's Security tab and select "Report a vulnerability". If that option
is not available to you, email hello@dkautomation.dev instead.

Do not open a public issue for a suspected vulnerability.

You should get a first response within 3 business days.

A good report includes:

- Steps to reproduce the issue
- The affected file (for example `reconcile.py`, a workflow file, or a sample
  data file)
- The impact: what someone could do with it

## Scope

### Treated as a vulnerability here

- The bank-statement or invoices file path arguments (`bank` and `invoices`
  in `reconcile.py`, read via `Path()` / `read_text()`) being usable to make
  `reconcile.py` read a file outside the directory it is meant to operate in
  (path traversal / arbitrary local file read), in particular if this tool is
  ever wired into an automated pipeline that accepts a filename from outside
  the operator.
- Any code path that executes or evaluates file content (bank statement or
  invoice data) instead of treating it strictly as plain CSV/text data.

### Not treated as a vulnerability here

- `reconcile.py` reading whatever local file path you explicitly pass it on
  your own command line when you run it yourself. Reading the file you point
  it at is the tool's normal job, not a vulnerability.
