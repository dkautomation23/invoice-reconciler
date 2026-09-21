# invoice-reconciler

[![CI](https://github.com/dkautomation23/invoice-reconciler/actions/workflows/ci.yml/badge.svg)](https://github.com/dkautomation23/invoice-reconciler/actions/workflows/ci.yml)

Matches a bank statement against your open invoices and tells you the only
thing that matters: **what still does not add up.** Short payments, duplicate
transfers, money with no invoice attached, invoices quietly going overdue.

```bash
python reconcile.py samples/bank_statement.csv samples/invoices.csv
```

No dependencies, no install, no data leaves the machine.

## Why

Every small business does this by hand once a month: bank export in one window,
invoice list in another, and an hour of scrolling to find the three lines that
disagree. The work is not hard, it is just tedious enough that it gets done late
— which is exactly when an unpaid invoice turns into a bad debt.

The matching itself is not one rule, it is five, applied strongest first. That
is the part people rebuild badly in spreadsheets.

## How it matches

| Pass | Evidence | Typical case |
| --- | --- | --- |
| 1 | invoice reference **and** amount agree | the normal, boring 90% |
| 2 | reference agrees, amount does not | short payment, overpayment, bank fee |
| 3 | exact amount + customer name in the description | client paid without quoting anything |
| 4 | several invoices adding up to one transfer | monthly bulk payment |
| 5 | several payments adding up to one invoice | agreed instalments |

References are compared digit-only, so `INV-2026-0142`, `RE 2026/0142` and a
bare `0142` all match the same invoice. Amounts are handled in cents, and both
`1.234,56` and `1,234.56` parse correctly.

Dates are read per file, not per row: the whole column is scanned first, so
`09/01/2026` is September in a US export and 9 January in a European one.
Guessing row by row is how an invoice that is not due yet shows up as 224 days
overdue.

A shortfall under €25 (or 2%) is reported as a **fee**, not a dispute. Chasing a
client over an €11.50 wire charge is how you lose them.

## Sample output

```console
$ python reconcile.py samples/bank_statement.csv samples/invoices.csv

Payments 9/11 matched   Invoices 7/11 settled
Money in 6,903.50   matched 6,428.50   still owed 3,701.50

Matched across several lines: 4 bank entries settling 3 invoice(s)

Needs a decision
  INV-2026-0143  Baker & Sons             fee_shortfall  1,188.50 of 1,200.00   short by 11.50 EUR
  INV-2026-0144  Delta Retail BV          underpaid      500.00 of 1,200.00   short by 700.00 EUR

Possible duplicate payments
  row 11 looks like row 10: 400.00 - SEPA CREDIT ACME CONSULTING monthly retainer

Money received, no invoice found
  row 11 2026-08-17     400.00 EUR   SEPA CREDIT ACME CONSULTING monthly retainer
  row 12 2026-08-18      75.00 EUR   REFUND FROM COURIER SERVICE

Overdue invoices
  INV-2026-0152  Pinewood Interiors         2,350.00 EUR  28 days late
  INV-2026-0143  Baker & Sons                  11.50 EUR  17 days late (part paid)
  INV-2026-0144  Delta Retail BV              700.00 EUR  16 days late (part paid)

Open but not yet due: 1 invoice(s), 640.00
```

The sample statement contains the awkward cases on purpose: a bank fee, a part
payment, one transfer covering two invoices, two instalments covering one, a
genuine double charge, an unidentified refund, and outgoing card payments that
must be ignored.

`samples/bank_us.csv` + `samples/invoices_us.csv` are the same exercise in the
other dialect — comma separated, `MM/DD/YYYY`, `Credit`/`Debit` columns,
`Doc No` instead of `InvoiceNumber` — to show the column matching and the date
detection doing their job:

```console
$ python reconcile.py samples/bank_us.csv samples/invoices_us.csv

Payments 4/5 matched   Invoices 3/4 settled
Money in 5,710.00   matched 5,490.00   still owed 1,180.00

Money received, no invoice found
  row 7 2026-08-07     220.00 USD   ACH DEPOSIT UNKNOWN SENDER

Open but not yet due: 1 invoice(s), 1,180.00
```

A part payment that a later transfer covers disappears from the exception list
on its own — only what is still open gets reported.

## Output for the accountant

```bash
python reconcile.py bank.csv invoices.csv --csv exceptions.csv --json report.json
```

`exceptions.csv` is one flat list of everything a human still has to handle —
issue, invoice, customer, amount, detail. Hand it over as-is.

The exit code is `1` when unresolved items exist and `0` when the month is
clean, so it slots into a nightly job:

```bash
python reconcile.py bank.csv invoices.csv --csv exceptions.csv || mail -s "Reconciliation exceptions" me@example.com < exceptions.csv
```

## Input format

Column names are matched loosely (`Amount`, `Betrag`, `Total`, `Gross` all work),
`;` and `,` separators are both accepted, and a UTF-8 BOM from Excel is handled.

Bank statement — needs a date, a description and an amount; negative rows are
treated as outgoing and skipped:

```csv
Date;Description;Amount;Currency
2026-08-03;SEPA CREDIT MERIDIAN LOGISTICS REF INV-2026-0142;1200,00;EUR
```

Invoices — needs a number and an amount; a due date enables the overdue report:

```csv
InvoiceNumber;Customer;IssueDate;DueDate;Amount;Currency
INV-2026-0142;Meridian Logistics;2026-07-20;2026-08-03;1200,00;EUR
```

## Honest limits

- **Currencies are not converted.** Mixed-currency files are flagged in the
  header line, but the totals are plain sums — convert first if you run
  multi-currency.
- Name matching is word overlap, not fuzzy scoring: "Baker & Sons" matches
  "BAKER AND SONS", but a typo in the client's own reference will not match.
- Batch and instalment passes look at up to 4 and 3 lines respectively — deeper
  combinations are left unmatched on purpose rather than guessed at.
- Read-only by design: it reads two CSVs and writes a report. It never touches
  your accounting system.

`python reconcile.py --selftest` runs 24 checks covering every pass, the amount
and date parsers, the settled-later case and the overdue maths.

## License

MIT
