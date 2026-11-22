"""AI invoice extractor: a folder of PDF invoices in, one checked Excel workbook out.

How it works
  1. Claude reads each PDF directly (text PDFs and scans alike) and returns the
     fields as a typed object (structured output, validated by Pydantic).
  2. Plain code then checks the arithmetic: line amounts = qty x unit price,
     lines add up to the subtotal, subtotal + tax = total.
  3. If a check fails, the model gets ONE second look with the exact errors.
     If the document itself is wrong, the printed values are kept and the
     invoice is flagged for a human, never silently "fixed".
  4. Duplicates (same vendor + invoice number) and non-invoices are flagged.
  5. Everything goes into an Excel workbook: Invoices, Line items, Checks.

Usage
  python extractor.py sample_invoices            # live: needs ANTHROPIC_API_KEY
  python extractor.py sample_invoices --replay   # no key: uses saved responses in recorded/
  python extractor.py sample_invoices --record   # live, and saves responses to recorded/
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

MODEL = os.environ.get("EXTRACTOR_MODEL", "claude-opus-5-5")
HERE = Path(__file__).resolve().parent
CENT = Decimal("0.01")


# --------------------------------------------------------------------------- schema
class LineItem(BaseModel):
    code: Optional[str] = Field(None, description="Item / SKU code exactly as printed, or null")
    description: str = Field(description="Line description exactly as printed")
    qty: str = Field(description="Quantity as printed, digits only, e.g. '4' or '2.5'")
    unit_price: str = Field(description="Unit price as a plain number, e.g. '52.90' (no currency sign, no commas)")
    amount: str = Field(description="Line amount as a plain number, e.g. '211.60'")


class Invoice(BaseModel):
    document_type: str = Field(description="'invoice', or what it is instead: 'bank_statement', 'receipt', 'other'")
    vendor: Optional[str] = Field(None, description="Company that issued the invoice")
    invoice_number: Optional[str] = None
    invoice_date: Optional[str] = Field(None, description="ISO date YYYY-MM-DD")
    due_date: Optional[str] = Field(None, description="ISO date YYYY-MM-DD, or null")
    reference: Optional[str] = Field(None, description="Customer PO / reference number, or null")
    currency: Optional[str] = Field(None, description="ISO code, e.g. USD")
    lines: list[LineItem] = Field(default_factory=list)
    subtotal: Optional[str] = Field(None, description="Printed subtotal, plain number")
    tax_rate: Optional[str] = Field(None, description="Tax rate in percent as printed, e.g. '8.25'")
    tax: Optional[str] = Field(None, description="Printed tax amount, plain number")
    total: Optional[str] = Field(None, description="Printed total due, plain number")
    reader_notes: Optional[str] = Field(None, description="Anything unclear or damaged on the page, else null")


PROMPT = """You are extracting data from one business document for an accounts-payable spreadsheet.

Read the whole document, including any scanned or rotated page.
Rules:
- Copy every value exactly as PRINTED. Never calculate, round or correct a number yourself:
  if the printed subtotal does not match the lines, still return the printed subtotal.
- Numbers as plain strings: no currency signs, no thousands separators ('1,250.00' -> '1250.00').
- Dates as YYYY-MM-DD.
- One entry in `lines` per printed line item; skip headers, subtotal, tax and total rows.
- If this is not an invoice (for example a bank statement), set document_type accordingly
  and leave the invoice fields null.
- Put anything you could not read clearly in reader_notes."""

RETRY = """Your extraction failed these arithmetic checks:
{errors}

Look at the document again, line by line. If you misread a value, return the corrected
extraction. If the document itself really prints these numbers, return the printed values
unchanged and say so in reader_notes. Never change a printed number to make the sums work."""


# --------------------------------------------------------------------------- checks
def dec(x) -> Optional[Decimal]:
    if x is None:
        return None
    try:
        return Decimal(str(x).replace(",", "").replace("$", "").strip())
    except InvalidOperation:
        return None


def check_invoice(inv: Invoice) -> list[str]:
    """Return a list of human-readable arithmetic problems (empty = all good)."""
    if inv.document_type != "invoice":
        return []
    errs: list[str] = []
    line_sum = Decimal("0")
    for i, ln in enumerate(inv.lines, 1):
        q, p, a = dec(ln.qty), dec(ln.unit_price), dec(ln.amount)
        if None in (q, p, a):
            errs.append(f"line {i}: a number could not be read (qty={ln.qty!r}, price={ln.unit_price!r}, amount={ln.amount!r})")
            continue
        if (q * p).quantize(CENT) != a.quantize(CENT):
            errs.append(f"line {i} '{ln.description[:40]}': {q} x {p} = {(q*p).quantize(CENT)}, printed {a}")
        line_sum += a
    sub, tax, tot = dec(inv.subtotal), dec(inv.tax), dec(inv.total)
    if sub is None or tot is None:
        errs.append("subtotal or total missing")
        return errs
    if inv.lines and line_sum.quantize(CENT) != sub.quantize(CENT):
        errs.append(f"lines add up to {line_sum.quantize(CENT)}, printed subtotal is {sub} (difference {(sub-line_sum).quantize(CENT)})")
    if (sub + (tax or 0)).quantize(CENT) != tot.quantize(CENT):
        errs.append(f"subtotal {sub} + tax {tax or 0} = {(sub+(tax or 0)).quantize(CENT)}, printed total is {tot}")
    rate = dec(inv.tax_rate)
    if rate is not None and tax is not None and abs((sub * rate / 100).quantize(CENT) - tax) > CENT:
        errs.append(f"tax {tax} is not {rate}% of {sub} ({(sub*rate/100).quantize(CENT)})")
    return errs


# --------------------------------------------------------------------------- model call
def _load_dotenv(path: Path = HERE / ".env") -> None:
    """Read KEY=value lines from .env (no extra dependency). Real env vars win."""
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _client():
    import anthropic

    _load_dotenv()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set. Copy .env.example, or run with --replay to use saved responses.")
    return anthropic.Anthropic()


def ask_model(client, pdf: Path, history: list) -> tuple[Invoice, dict, list]:
    if not history:
        data = base64.standard_b64encode(pdf.read_bytes()).decode()
        history = [{
            "role": "user",
            "content": [
                {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data}},
                {"type": "text", "text": PROMPT},
            ],
        }]
    resp = client.messages.parse(
        model=MODEL,
        max_tokens=16000,
        output_config={"effort": "high"},
        messages=history,
        output_format=Invoice,
    )
    inv = resp.parsed_output
    usage = {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens}
    history = history + [{"role": "assistant", "content": inv.model_dump_json()}]
    return inv, usage, history


def extract_one(pdf: Path, mode: str, client=None) -> dict:
    rec_file = HERE / "recorded" / (pdf.stem + ".json")
    if mode == "replay":
        if not rec_file.exists():
            raise FileNotFoundError(f"no saved response for {pdf.name} in recorded/")
        saved = json.loads(rec_file.read_text(encoding="utf-8"))
        saved["final"] = Invoice.model_validate(saved["final"])
        saved["checks"] = check_invoice(saved["final"])
        return saved

    t0 = time.time()
    inv, usage, hist = ask_model(client, pdf, [])
    attempts = [{"extraction": inv.model_dump(), "checks": check_invoice(inv), "usage": usage}]
    errs = attempts[0]["checks"]
    if errs:  # one second look, with the exact errors
        hist = hist + [{"role": "user", "content": RETRY.format(errors="\n".join("- " + e for e in errs))}]
        inv, usage, hist = ask_model(client, pdf, hist)
        attempts.append({"extraction": inv.model_dump(), "checks": check_invoice(inv), "usage": usage})
    result = {"file": pdf.name, "model": MODEL, "seconds": round(time.time() - t0, 1),
              "attempts": attempts, "final": inv, "checks": check_invoice(inv)}
    if mode == "record":
        rec_file.parent.mkdir(exist_ok=True)
        dump = dict(result, final=inv.model_dump())
        rec_file.write_text(json.dumps(dump, indent=2), encoding="utf-8")
    return result


# --------------------------------------------------------------------------- excel
def write_excel(results: list[dict], out: Path) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    head = Font(bold=True, color="FFFFFF")
    fill = PatternFill("solid", fgColor="2F5597")
    red = PatternFill("solid", fgColor="FDE2E1")
    green = PatternFill("solid", fgColor="E2F5E6")

    def sheet(ws, cols):
        ws.append(cols)
        for c in ws[1]:
            c.font, c.fill = head, fill
        ws.freeze_panes = "A2"

    def widths(ws):
        for col in ws.columns:
            w = max(len(str(c.value or "")) for c in col)
            ws.column_dimensions[get_column_letter(col[0].column)].width = min(max(10, w + 2), 60)
        # print/PDF: landscape, one page wide, header row repeated
        ws.page_setup.orientation = "landscape"
        ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
        ws.sheet_properties.pageSetUpPr.fitToPage = True
        ws.print_title_rows = "1:1"

    ws = wb.active
    ws.title = "Invoices"
    sheet(ws, ["File", "Vendor", "Invoice no.", "Date", "Due", "Reference", "Lines",
               "Subtotal", "Tax %", "Tax", "Total", "Status", "Why"])
    for r in results:
        inv: Invoice = r["final"]
        ok = r["status"] == "OK"
        ws.append([r["file"], inv.vendor, inv.invoice_number, inv.invoice_date, inv.due_date, inv.reference,
                   len(inv.lines), _num(inv.subtotal), _num(inv.tax_rate), _num(inv.tax), _num(inv.total),
                   r["status"], "; ".join(r["why"])])
        ws.cell(ws.max_row, 12).fill = green if ok else red
    for row in ws.iter_rows(min_row=2, min_col=8, max_col=11):
        for c in row:
            c.number_format = "#,##0.00"
    widths(ws)

    ws2 = wb.create_sheet("Line items")
    sheet(ws2, ["File", "Invoice no.", "Code", "Description", "Qty", "Unit price", "Amount"])
    for r in results:
        inv = r["final"]
        for ln in inv.lines:
            ws2.append([r["file"], inv.invoice_number, ln.code, ln.description,
                        _num(ln.qty), _num(ln.unit_price), _num(ln.amount)])
    widths(ws2)

    ws3 = wb.create_sheet("Checks")
    sheet(ws3, ["File", "Check result", "Model looks", "Seconds", "Reader notes"])
    for r in results:
        ws3.append([r["file"], "; ".join(r["why"]) or "all sums agree", len(r.get("attempts", [])) or "",
                    r.get("seconds", ""), r["final"].reader_notes])
        ws3.cell(ws3.max_row, 2).alignment = Alignment(wrap_text=True)
    widths(ws3)
    wb.save(out)


def _num(x):
    d = dec(x)
    return float(d) if d is not None else x


# --------------------------------------------------------------------------- main
def run(folder: Path, mode: str, out: Path) -> list[dict]:
    pdfs = sorted(folder.glob("*.pdf"))
    if not pdfs:
        sys.exit(f"no PDF files in {folder}")
    client = None if mode == "replay" else _client()
    results, groups = [], {}
    for pdf in pdfs:
        r = extract_one(pdf, mode, client)
        inv: Invoice = r["final"]
        r["why"] = list(r["checks"])
        if inv.document_type != "invoice":
            r["why"] = [f"not an invoice ({inv.document_type}), left out of the totals"]
        else:
            key = ((inv.vendor or "").lower().strip(), (inv.invoice_number or "").strip())
            groups.setdefault(key, []).append(r)
        results.append(r)
    for copies in groups.values():  # the same invoice twice: flag every copy, name the others
        if len(copies) > 1:
            for r in copies:
                others = ", ".join(o["file"] for o in copies if o is not r)
                r["why"].append(f"DUPLICATE: the same invoice is also in {others}. Pay it once")
    for r in results:
        why = r["why"]
        r["status"] = "OK" if not why else ("SKIPPED" if r["final"].document_type != "invoice" else "CHECK")
        print(f"{r['status']:8} {r['file']}" + (f"  -> {why[0]}" if why else ""))
    write_excel(results, out)
    ok = sum(r["status"] == "OK" for r in results)
    print(f"\n{len(results)} documents: {ok} OK, {len(results) - ok} flagged for a human. Workbook: {out}")
    return results


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", type=Path)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--replay", action="store_true", help="use saved responses in recorded/ (no API key needed)")
    g.add_argument("--record", action="store_true", help="call the API and save responses to recorded/")
    ap.add_argument("--out", type=Path, default=HERE / "output" / "invoices.xlsx")
    a = ap.parse_args(argv)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    run(a.folder, "replay" if a.replay else "record" if a.record else "live", a.out)


if __name__ == "__main__":
    main()
