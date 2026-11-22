"""Tests run with no API key: they use the saved model responses in recorded/."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import extractor as ex  # noqa: E402
import score_accuracy  # noqa: E402


def make(lines, subtotal, tax, total, rate=None):
    return ex.Invoice(document_type="invoice", vendor="V", invoice_number="1",
                      lines=[ex.LineItem(description=d, qty=q, unit_price=p, amount=a) for d, q, p, a in lines],
                      subtotal=subtotal, tax=tax, total=total, tax_rate=rate)


def test_clean_invoice_passes():
    inv = make([("a", "2", "10.00", "20.00"), ("b", "1", "5.50", "5.50")], "25.50", "2.04", "27.54", "8")
    assert ex.check_invoice(inv) == []


def test_line_sum_mismatch_is_caught():
    inv = make([("a", "2", "10.00", "20.00")], "70.00", "0", "70.00")
    errs = ex.check_invoice(inv)
    assert any("lines add up to 20.00" in e for e in errs)


def test_bad_line_math_is_caught():
    inv = make([("a", "3", "10.00", "20.00")], "20.00", "0", "20.00")
    assert any("3 x 10.00 = 30.00" in e for e in ex.check_invoice(inv))


def test_total_mismatch_is_caught():
    inv = make([("a", "1", "10.00", "10.00")], "10.00", "1.00", "12.00")
    assert any("printed total is 12.00" in e for e in ex.check_invoice(inv))


def test_tax_rate_mismatch_is_caught():
    inv = make([("a", "1", "100.00", "100.00")], "100.00", "9.00", "109.00", "8.25")
    assert any("is not 8.25%" in e for e in ex.check_invoice(inv))


def test_non_invoice_is_not_checked():
    assert ex.check_invoice(ex.Invoice(document_type="bank_statement")) == []


def test_numbers_with_symbols_parse():
    assert ex.dec("$1,250.00") == ex.dec("1250.00")


@pytest.fixture(scope="module")
def replay(tmp_path_factory):
    out = tmp_path_factory.mktemp("out") / "invoices.xlsx"
    results = ex.run(ROOT / "sample_invoices", "replay", out)
    return {r["file"]: r for r in results}, out


def test_replay_flags_planted_subtotal_error(replay):
    results, _ = replay
    r = results["Kestrelpoint invoice KD-2026-034.pdf"]
    assert r["status"] == "CHECK"
    assert any("difference 50.00" in w for w in r["why"])


def test_replay_flags_both_copies_of_duplicate(replay):
    results, _ = replay
    for f in ("INV_HP-10432.pdf", "HP-10432 resent.pdf"):
        assert any("DUPLICATE" in w for w in results[f]["why"]), f


def test_replay_skips_bank_statement(replay):
    results, _ = replay
    assert results["Statement_Mar2026_Lanternfield.pdf"]["status"] == "SKIPPED"


def test_replay_scan_reads_clean(replay):
    results, _ = replay
    assert results["scan_0037.pdf"]["status"] == "OK"


def test_replay_writes_three_sheets(replay):
    from openpyxl import load_workbook

    _, out = replay
    wb = load_workbook(out)
    assert wb.sheetnames == ["Invoices", "Line items", "Checks"]
    assert wb["Invoices"].max_row == 15  # header + 14 documents


def test_second_look_is_recorded_for_the_failed_invoice():
    rec = json.loads((ROOT / "recorded" / "Kestrelpoint invoice KD-2026-034.json").read_text(encoding="utf-8"))
    assert len(rec["attempts"]) == 2
    assert rec["attempts"][0]["checks"] and rec["final"]["subtotal"] == "2991.50"


def test_accuracy_against_answer_key():
    r = score_accuracy.score()
    assert r["fields_checked"] == 553
    assert r["accuracy_pct"] == 100.0
