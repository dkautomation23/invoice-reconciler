"""Match a bank statement against open invoices and report what does not add up.

    python reconcile.py samples/bank_statement.csv samples/invoices.csv
    python reconcile.py bank.csv invoices.csv --csv exceptions.csv --json report.json
    python reconcile.py --selftest

Five passes, strongest evidence first: invoice number + exact amount, invoice
number alone, exact amount + customer name, several invoices paid in one
transfer, one invoice paid in instalments. Whatever survives all five is what a
human actually has to look at.

Standard library only - no pandas, no install, runs on any Python 3.10+.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from itertools import combinations
from pathlib import Path

# Anything that could be an invoice reference inside a payment description:
# INV-2026-0142, RE 2026/0142, FV/2026/142, 2026-0142, a bare 0142.
REFERENCE_CHUNK = re.compile(r"[A-Za-z]{0,6}[-/ ]?\d[\d\-/ ]{2,}")
YEAR_RE = re.compile(r"^(19|20)\d{2}$")
WORD_RE = re.compile(r"[a-z0-9]+")

# A shortfall this small is almost always a transfer fee, not a dispute.
FEE_TOLERANCE_ABS = 25.0
FEE_TOLERANCE_PCT = 0.02
# Amounts are compared in cents to keep float noise out of the matching.
CENT = 100


def parse_amount(raw) -> int:
    """'1.234,56' / '1,234.56' / '-89.00' -> cents (int). Junk -> 0."""
    text = str(raw or "").strip()
    if not text:
        return 0
    negative = text.startswith("-") or (text.startswith("(") and text.endswith(")"))
    digits = re.sub(r"[^\d.,]", "", text)
    if not digits:
        return 0
    if "," in digits and "." in digits:
        if digits.rfind(",") > digits.rfind("."):
            digits = digits.replace(".", "").replace(",", ".")
        else:
            digits = digits.replace(",", "")
    elif "," in digits:
        digits = digits.replace(",", ".") if len(digits.split(",")[-1]) == 2 else digits.replace(",", "")
    try:
        value = round(float(digits) * CENT)
    except ValueError:
        return 0
    return -value if negative else value


SLASHED_RE = re.compile(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})$")


def detect_date_order(values) -> str:
    """Decide whether a column is day-first or month-first, using the column.

    03/04/2026 is ambiguous on its own; 13/04/2026 anywhere in the same file is
    not. Guessing per row is how a July invoice ends up 224 days overdue.
    """
    day_first = month_first = 0
    for value in values:
        match = SLASHED_RE.match(str(value or "").strip()[:10])
        if not match:
            continue
        first, second = int(match.group(1)), int(match.group(2))
        if first > 12 >= second:
            day_first += 1
        elif second > 12 >= first:
            month_first += 1
    if month_first > day_first:
        return "mdy"
    return "dmy"                     # the European default, and the ISO tie-break


def parse_date(raw, order: str = "dmy") -> date | None:
    text = str(raw or "").strip()[:10]
    if not text:
        return None
    ordered = ("%m/%d/%Y", "%m-%d-%Y") if order == "mdy" else ("%d/%m/%Y", "%d-%m-%Y")
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d.%m.%Y", *ordered):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def money(cents: int, currency: str = "") -> str:
    return f"{cents / CENT:,.2f}{(' ' + currency) if currency else ''}"


def words(text: str) -> set[str]:
    """Words worth matching a customer name on - short noise removed."""
    stop = {"gmbh", "ltd", "llc", "bv", "sa", "inc", "the", "and", "payment", "invoice", "transfer", "ref"}
    return {w for w in WORD_RE.findall(str(text).lower()) if len(w) > 2 and w not in stop}


def ref_forms(raw: str) -> set[str]:
    """Digit-only forms of one reference, so INV-2026-0142 == 2026/0142 == 0142.

    Both sides of the comparison go through this, which is the only reason the
    matching survives the fact that nobody writes references consistently.
    """
    digits = re.sub(r"\D", "", str(raw))
    if len(digits) < 3:
        return set()
    forms = {digits, digits.lstrip("0") or digits}
    if len(digits) > 4:
        tail = digits[-4:]                      # the sequential part, e.g. 0142
        forms.add(tail)
        forms.add(tail.lstrip("0") or tail)
    return {form for form in forms if len(form) >= 3 and not YEAR_RE.match(form)}


def references(text: str) -> set[str]:
    """Every reference-looking token in a payment description."""
    found: set[str] = set()
    for chunk in REFERENCE_CHUNK.findall(str(text)):
        found |= ref_forms(chunk)
    return found


@dataclass
class Payment:
    row: int
    date: date | None
    description: str
    amount: int                 # cents, positive = money in
    currency: str = ""
    matched_to: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"P{self.row}"


@dataclass
class Invoice:
    row: int
    number: str
    customer: str
    issue_date: date | None
    due_date: date | None
    amount: int                 # cents
    currency: str = ""
    paid: int = 0               # cents received so far
    matched_by: list[str] = field(default_factory=list)

    @property
    def open_amount(self) -> int:
        return self.amount - self.paid

    def tokens(self) -> set[str]:
        return ref_forms(self.number)


def read_rows(path: Path) -> list[dict]:
    """CSV reader that tolerates ; separators and a UTF-8 BOM."""
    text = path.read_text(encoding="utf-8-sig")
    sample = text[:2000]
    delimiter = ";" if sample.count(";") > sample.count(",") else ","
    return list(csv.DictReader(text.splitlines(), delimiter=delimiter))


def column(row: dict, *names: str) -> str:
    """First column whose header contains one of `names` (case-insensitive)."""
    lowered = {str(key).strip().lower(): value for key, value in row.items() if key}
    for name in names:
        for header, value in lowered.items():
            if name in header:
                return value or ""
    return ""


def load_payments(path: Path) -> list[Payment]:
    payments = []
    rows = read_rows(path)
    order = detect_date_order(column(row, "date", "booked", "datum") for row in rows)
    for index, row in enumerate(rows, start=2):
        amount = parse_amount(column(row, "amount", "credit", "value", "betrag", "sum"))
        if amount <= 0:
            continue                      # outgoing lines are not customer payments
        payments.append(Payment(
            row=index,
            date=parse_date(column(row, "date", "booked", "datum"), order),
            description=column(row, "description", "reference", "details", "purpose", "narrative", "text"),
            amount=amount,
            currency=column(row, "currency", "ccy").upper(),
        ))
    return payments


def load_invoices(path: Path) -> list[Invoice]:
    invoices = []
    rows = read_rows(path)
    # Both date columns come from the same system, so they share one convention.
    order = detect_date_order(
        [column(row, "due", "fällig", "faellig") for row in rows]
        + [column(row, "issue", "date", "datum") for row in rows]
    )
    for index, row in enumerate(rows, start=2):
        number = column(row, "invoice", "number", "nummer", "doc").strip()
        amount = parse_amount(column(row, "amount", "total", "gross", "betrag"))
        if not number or amount <= 0:
            continue
        invoices.append(Invoice(
            row=index,
            number=number,
            customer=column(row, "customer", "client", "name", "kunde", "payer"),
            issue_date=parse_date(column(row, "issue", "date", "datum"), order),
            due_date=parse_date(column(row, "due", "fällig", "faellig"), order),
            amount=amount,
            currency=column(row, "currency", "ccy").upper(),
        ))
    return invoices


def is_fee(shortfall: int, invoice_amount: int) -> bool:
    """Small shortfall on an otherwise correct payment = bank/FX fee."""
    return 0 < shortfall <= max(FEE_TOLERANCE_ABS * CENT, invoice_amount * FEE_TOLERANCE_PCT)


def reconcile(payments: list[Payment], invoices: list[Invoice], today: date | None = None) -> dict:
    """Run the five passes and build the report."""
    today = today or date.today()
    matches: list[dict] = []
    by_token: dict[str, list[Invoice]] = {}
    for invoice in invoices:
        for token in invoice.tokens():
            by_token.setdefault(token, []).append(invoice)

    def link(payment: Payment, invoice: Invoice, amount: int, kind: str, confidence: str, note: str = "") -> None:
        invoice.paid += amount
        invoice.matched_by.append(payment.key)
        payment.matched_to.append(invoice.number)
        matches.append({
            "payment_row": payment.row, "payment_date": str(payment.date or ""),
            "invoice": invoice.number, "customer": invoice.customer,
            "amount": amount, "invoice_amount": invoice.amount,
            "type": kind, "confidence": confidence, "note": note,
        })

    open_payments = lambda: [p for p in payments if not p.matched_to]          # noqa: E731
    open_invoices = lambda: [i for i in invoices if i.open_amount > 0]         # noqa: E731

    # Pass 1 - reference and amount both agree. Nothing to argue with.
    for payment in open_payments():
        for token in references(payment.description):
            for invoice in by_token.get(token, []):
                if invoice.open_amount == payment.amount:
                    link(payment, invoice, payment.amount, "exact", "high", f"reference '{token}'")
                    break
            if payment.matched_to:
                break

    # Pass 2 - reference agrees, amount does not: short payment, overpayment or fee.
    for payment in open_payments():
        for token in references(payment.description):
            candidates = [i for i in by_token.get(token, []) if i.open_amount > 0]
            if not candidates:
                continue
            invoice = candidates[0]
            difference = invoice.open_amount - payment.amount
            if difference > 0:
                kind = "fee_shortfall" if is_fee(difference, invoice.amount) else "underpaid"
                note = f"short by {money(difference, invoice.currency)}"
            elif difference < 0:
                kind, note = "overpaid", f"over by {money(-difference, invoice.currency)}"
            else:
                kind, note = "exact", "reference match"
            link(payment, invoice, payment.amount, kind, "high", note)
            break

    # Pass 3 - no reference, but the amount is exact and the name lines up.
    for payment in open_payments():
        payment_words = words(payment.description)
        for invoice in open_invoices():
            if invoice.open_amount != payment.amount:
                continue
            overlap = payment_words & words(invoice.customer)
            if overlap:
                link(payment, invoice, payment.amount, "amount_and_name", "medium",
                     f"matched on '{', '.join(sorted(overlap))}'")
                break

    # Pass 4 - one transfer settling several invoices from the same customer.
    for payment in open_payments():
        payment_words = words(payment.description)
        candidates = [i for i in open_invoices() if payment_words & words(i.customer)]
        if len(candidates) < 2:
            continue
        found = None
        for size in (2, 3, 4):
            for combo in combinations(candidates, size):
                if sum(i.open_amount for i in combo) == payment.amount:
                    found = combo
                    break
            if found:
                break
        if found:
            for invoice in found:
                link(payment, invoice, invoice.open_amount, "batch_payment", "medium",
                     f"one transfer covering {len(found)} invoices")

    # Pass 5 - instalments: several payments adding up to one invoice.
    for invoice in open_invoices():
        invoice_words = words(invoice.customer)
        parts = [p for p in open_payments() if invoice_words & words(p.description)]
        if len(parts) < 2:
            continue
        for size in (2, 3):
            for combo in combinations(parts, size):
                if sum(p.amount for p in combo) == invoice.open_amount:
                    for payment in combo:
                        link(payment, invoice, payment.amount, "instalment", "medium",
                             f"part {combo.index(payment) + 1} of {len(combo)}")
                    break
            if invoice.open_amount <= 0:
                break

    # Duplicate incoming payments: same amount, same description, different rows.
    seen: dict[tuple, Payment] = {}
    duplicates = []
    for payment in payments:
        signature = (payment.amount, " ".join(sorted(words(payment.description))))
        earlier = seen.get(signature)
        if earlier and payment.description:
            # Same amount and wording is only suspicious when it lands within a
            # few days - instalments a fortnight apart are not double charges.
            gap = abs((payment.date - earlier.date).days) if payment.date and earlier.date else 0
            if gap <= 3:
                duplicates.append({"row": payment.row, "duplicate_of": earlier.row,
                                   "amount": payment.amount, "description": payment.description})
                continue
        seen[signature] = payment

    unmatched_payments = [
        {"row": p.row, "date": str(p.date or ""), "amount": p.amount,
         "currency": p.currency, "description": p.description}
        for p in payments if not p.matched_to
    ]
    outstanding = []
    for invoice in invoices:
        if invoice.open_amount <= 0:
            continue
        overdue_days = (today - invoice.due_date).days if invoice.due_date else None
        outstanding.append({
            "invoice": invoice.number, "customer": invoice.customer,
            "amount": invoice.amount, "open": invoice.open_amount,
            "currency": invoice.currency, "due": str(invoice.due_date or ""),
            "overdue_days": overdue_days if (overdue_days or 0) > 0 else 0,
            "partly_paid": invoice.paid > 0,
        })
    outstanding.sort(key=lambda item: (-(item["overdue_days"] or 0), -item["open"]))

    currencies = {i.currency for i in invoices if i.currency} | {p.currency for p in payments if p.currency}

    return {
        "matches": matches,
        "duplicates": duplicates,
        "unmatched_payments": unmatched_payments,
        "outstanding": outstanding,
        "totals": {
            "payments_seen": len(payments),
            "payments_matched": len([p for p in payments if p.matched_to]),
            "invoices_seen": len(invoices),
            "invoices_settled": len([i for i in invoices if i.open_amount <= 0]),
            "money_in": sum(p.amount for p in payments),
            "money_matched": sum(m["amount"] for m in matches),
            "money_outstanding": sum(item["open"] for item in outstanding),
            "currencies": sorted(currencies),
        },
    }


def print_report(report: dict) -> None:
    totals = report["totals"]
    print(f"\nPayments {totals['payments_matched']}/{totals['payments_seen']} matched   "
          f"Invoices {totals['invoices_settled']}/{totals['invoices_seen']} settled")
    print(f"Money in {money(totals['money_in'])}   matched {money(totals['money_matched'])}   "
          f"still owed {money(totals['money_outstanding'])}")
    if len(totals["currencies"]) > 1:
        print(f"NOTE: several currencies present ({', '.join(totals['currencies'])}) - "
              "amounts above are added up as plain numbers, convert before trusting them")

    # A batch payment or a set of instalments that adds up is a solved case,
    # not an exception - it is worth one line, not a list.
    combined = [m for m in report["matches"] if m["type"] in {"batch_payment", "instalment"}]
    if combined:
        print(f"\nMatched across several lines: {len(combined)} bank entries settling "
              f"{len({m['invoice'] for m in combined})} invoice(s)")

    # A shortfall that a later transfer covered is history, not an open item.
    still_open = {item["invoice"] for item in report["outstanding"]}
    problems = [m for m in report["matches"]
                if m["type"] not in {"exact", "amount_and_name", "batch_payment", "instalment"}
                and (m["type"] == "overpaid" or m["invoice"] in still_open)]
    if problems:
        print("\nNeeds a decision")
        for match in problems:
            print(f"  {match['invoice']:<14} {match['customer'][:22]:<24} "
                  f"{match['type']:<14} {money(match['amount'])} of {money(match['invoice_amount'])}"
                  f"   {match['note']}")

    if report["duplicates"]:
        print("\nPossible duplicate payments")
        for item in report["duplicates"]:
            print(f"  row {item['row']} looks like row {item['duplicate_of']}: "
                  f"{money(item['amount'])} - {item['description'][:60]}")

    if report["unmatched_payments"]:
        print("\nMoney received, no invoice found")
        for item in report["unmatched_payments"]:
            print(f"  row {item['row']} {item['date']} {money(item['amount'], item['currency']):>14}"
                  f"   {item['description'][:64]}")

    overdue = [item for item in report["outstanding"] if item["overdue_days"]]
    if overdue:
        print("\nOverdue invoices")
        for item in overdue:
            note = " (part paid)" if item["partly_paid"] else ""
            print(f"  {item['invoice']:<14} {item['customer'][:22]:<24} "
                  f"{money(item['open'], item['currency']):>14}  {item['overdue_days']} days late{note}")

    not_due = [item for item in report["outstanding"] if not item["overdue_days"]]
    if not_due:
        print(f"\nOpen but not yet due: {len(not_due)} invoice(s), "
              f"{money(sum(i['open'] for i in not_due))}")
    print()


def write_exceptions_csv(report: dict, path: Path) -> None:
    """One flat file with everything a human still has to handle."""
    rows = []
    still_open = {item["invoice"] for item in report["outstanding"]}
    for match in report["matches"]:
        if match["type"] in {"exact", "amount_and_name", "batch_payment", "instalment"}:
            continue
        if match["type"] != "overpaid" and match["invoice"] not in still_open:
            continue                      # settled later by another payment
        rows.append({"issue": match["type"], "invoice": match["invoice"], "customer": match["customer"],
                     "amount": f"{match['amount'] / CENT:.2f}", "detail": match["note"]})
    for item in report["duplicates"]:
        rows.append({"issue": "duplicate_payment", "invoice": "", "customer": "",
                     "amount": f"{item['amount'] / CENT:.2f}",
                     "detail": f"row {item['row']} duplicates row {item['duplicate_of']}: {item['description']}"})
    for item in report["unmatched_payments"]:
        rows.append({"issue": "unidentified_payment", "invoice": "", "customer": "",
                     "amount": f"{item['amount'] / CENT:.2f}",
                     "detail": f"row {item['row']} {item['date']} {item['description']}"})
    for item in report["outstanding"]:
        if item["overdue_days"]:
            rows.append({"issue": "overdue", "invoice": item["invoice"], "customer": item["customer"],
                         "amount": f"{item['open'] / CENT:.2f}",
                         "detail": f"{item['overdue_days']} days past {item['due']}"})

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["issue", "invoice", "customer", "amount", "detail"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"{len(rows)} exception(s) written to {path}")


def selftest() -> int:
    """Builds small statements in memory and checks each pass separately."""
    checks, failures = 0, []

    def check(label, condition):
        nonlocal checks
        checks += 1
        if not condition:
            failures.append(label)

    def payment(row, description, amount, day="2026-08-10"):
        return Payment(row=row, date=parse_date(day), description=description, amount=parse_amount(amount))

    def invoice(row, number, customer, amount, due="2026-08-01"):
        return Invoice(row=row, number=number, customer=customer, issue_date=parse_date("2026-07-01"),
                       due_date=parse_date(due), amount=parse_amount(amount))

    check("amount parser: European", parse_amount("1.234,56") == 123456)
    check("amount parser: US", parse_amount("1,234.56") == 123456)
    check("amount parser: negative", parse_amount("-89.00") == -8900)
    check("amount parser: junk", parse_amount("n/a") == 0)
    check("date parser handles dotted dates", parse_date("05.08.2026") == date(2026, 8, 5))
    check("column with a >12 day reads as day-first",
          detect_date_order(["13/04/2026", "09/01/2026"]) == "dmy")
    check("column with a >12 month position reads as month-first",
          detect_date_order(["07/28/2026", "09/01/2026"]) == "mdy")
    check("US column parses 09/01 as September",
          parse_date("09/01/2026", detect_date_order(["07/28/2026", "09/01/2026"])) == date(2026, 9, 1))

    report = reconcile([payment(2, "Payment INV-2026-0142 Meridian", "1200.00")],
                       [invoice(2, "INV-2026-0142", "Meridian Logistics", "1200.00")],
                       today=date(2026, 8, 20))
    check("pass 1 matches reference + amount", report["matches"][0]["type"] == "exact")
    check("pass 1 leaves nothing outstanding", report["totals"]["money_outstanding"] == 0)

    report = reconcile([payment(2, "INV-2026-0143", "1188.50")],
                       [invoice(2, "INV-2026-0143", "Baker & Sons", "1200.00")],
                       today=date(2026, 8, 20))
    check("small shortfall reads as a fee", report["matches"][0]["type"] == "fee_shortfall")

    report = reconcile([payment(2, "INV-2026-0144", "500.00")],
                       [invoice(2, "INV-2026-0144", "Delta", "1200.00")],
                       today=date(2026, 8, 20))
    check("large shortfall reads as underpaid", report["matches"][0]["type"] == "underpaid")
    check("underpaid invoice stays open", report["outstanding"][0]["open"] == parse_amount("700.00"))
    check("part payment is flagged", report["outstanding"][0]["partly_paid"] is True)

    report = reconcile([payment(2, "Transfer from Brightside Retail Ltd", "890.00")],
                       [invoice(2, "INV-2026-0150", "Brightside Retail", "890.00")],
                       today=date(2026, 8, 20))
    check("pass 3 matches on amount + name", report["matches"][0]["type"] == "amount_and_name")

    report = reconcile([payment(2, "Nordwind Supplies bulk transfer", "300.00")],
                       [invoice(2, "INV-1", "Nordwind Supplies", "100.00"),
                        invoice(3, "INV-2", "Nordwind Supplies", "200.00")],
                       today=date(2026, 8, 20))
    check("pass 4 splits a batch payment", len(report["matches"]) == 2)
    check("batch payment clears both invoices", report["totals"]["invoices_settled"] == 2)

    report = reconcile([payment(2, "Kestrel Design part 1", "250.00"),
                        payment(3, "Kestrel Design part 2", "250.00")],
                       [invoice(2, "INV-9", "Kestrel Design", "500.00")],
                       today=date(2026, 8, 20))
    check("pass 5 joins instalments", report["totals"]["invoices_settled"] == 1)

    report = reconcile([payment(2, "INV-2026-0160 first transfer", "1500.00", "2026-08-03"),
                        payment(3, "INV-2026-0160 balance", "1000.00", "2026-08-05")],
                       [invoice(2, "INV-2026-0160", "Harbor & Co", "2500.00")],
                       today=date(2026, 8, 20))
    check("a shortfall covered later settles the invoice", report["totals"]["invoices_settled"] == 1)
    check("nothing is left owing on it", report["totals"]["money_outstanding"] == 0)

    report = reconcile([payment(2, "Consulting fee ACME", "400.00"),
                        payment(3, "Consulting fee ACME", "400.00")],
                       [invoice(2, "INV-7", "ACME", "400.00")],
                       today=date(2026, 8, 20))
    check("duplicate payment is reported", len(report["duplicates"]) == 1)

    report = reconcile([payment(2, "Refund from supplier", "75.00")], [], today=date(2026, 8, 20))
    check("unidentified money is listed", len(report["unmatched_payments"]) == 1)

    report = reconcile([], [invoice(2, "INV-8", "Late Ltd", "900.00", due="2026-07-15")],
                       today=date(2026, 8, 20))
    check("overdue days are counted", report["outstanding"][0]["overdue_days"] == 36)

    report = reconcile([], [invoice(2, "INV-8", "Future Ltd", "900.00", due="2026-09-30")],
                       today=date(2026, 8, 20))
    check("not-yet-due invoices are not called overdue", report["outstanding"][0]["overdue_days"] == 0)

    print(f"selftest: {checks - len(failures)}/{checks} passed")
    for failure in failures:
        print("  FAILED:", failure)
    return 1 if failures else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Reconcile a bank statement against open invoices.")
    parser.add_argument("bank", nargs="?", help="bank statement CSV")
    parser.add_argument("invoices", nargs="?", help="invoice list CSV")
    parser.add_argument("--csv", type=Path, help="write the exception list here")
    parser.add_argument("--json", type=Path, help="write the full report as JSON")
    parser.add_argument("--selftest", action="store_true", help="run the built-in checks and exit")
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()
    if not (args.bank and args.invoices):
        parser.error("pass a bank CSV and an invoice CSV, or use --selftest")

    payments = load_payments(Path(args.bank))
    invoices = load_invoices(Path(args.invoices))
    if not payments:
        print("no incoming payments found - check the amount column in the statement")
    if not invoices:
        sys.exit("no invoices found - check the invoice number and amount columns")

    report = reconcile(payments, invoices)
    print_report(report)

    if args.csv:
        write_exceptions_csv(report, args.csv)
    if args.json:
        args.json.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print(f"full report written to {args.json}")

    # Non-zero exit when a human still has to act: useful in a nightly job.
    unresolved = (len(report["unmatched_payments"]) + len(report["duplicates"])
                  + len([m for m in report["matches"] if m["type"] in {"underpaid", "overpaid"}]))
    return 1 if unresolved else 0


if __name__ == "__main__":
    sys.exit(main())
