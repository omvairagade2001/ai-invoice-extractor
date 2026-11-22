"""Measure field accuracy of the saved extractions against the answer key.

The answer key (tests/answer_key.json) holds the values printed on each synthetic
document. It is used ONLY here, to measure; the extractor never sees it.

    python score_accuracy.py            # score after the arithmetic check (+ second look)
    python score_accuracy.py --first-pass  # score of the model's first reading only
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
HEADER = ["vendor", "invoice_number", "invoice_date", "due_date", "reference",
          "subtotal", "tax_rate", "tax", "total"]
LINE = ["code", "description", "qty", "unit_price", "amount"]
NUMERIC = {"subtotal", "tax_rate", "tax", "total", "qty", "unit_price", "amount"}


def same(field, got, want) -> bool:
    if want is None:
        return got in (None, "")
    if got is None:
        return False
    if field in NUMERIC:
        try:
            return Decimal(str(got).replace(",", "")) == Decimal(str(want).replace(",", ""))
        except Exception:
            return False
    return " ".join(str(got).split()).lower() == " ".join(str(want).split()).lower()


def score(key_path=HERE / "tests" / "answer_key.json", rec_dir=HERE / "recorded", final_key="final",
          first_pass=False) -> dict:
    key = json.loads(Path(key_path).read_text(encoding="utf-8"))["documents"]
    total = right = 0
    misses, docs = [], 0
    for fname, want in key.items():
        rec = json.loads((Path(rec_dir) / (Path(fname).stem + ".json")).read_text(encoding="utf-8"))
        if first_pass and rec.get("attempts"):
            got = rec["attempts"][0]["extraction"]
        else:
            got = rec[final_key] if final_key in rec else rec
        docs += 1
        if want["type"] != "invoice":
            ok = got["document_type"] != "invoice"
            total += 1
            right += ok
            if not ok:
                misses.append(f"{fname}: a {want['type']} was read as an invoice")
            continue
        for f in HEADER:
            total += 1
            if same(f, got.get(f), want.get(f)):
                right += 1
            else:
                misses.append(f"{fname}: {f} = {got.get(f)!r}, printed {want.get(f)!r}")
        wl, gl = want["lines"], got.get("lines", [])
        if len(wl) != len(gl):
            misses.append(f"{fname}: {len(gl)} lines read, {len(wl)} printed")
        for i, wline in enumerate(wl):
            gline = gl[i] if i < len(gl) else {}
            for f in LINE:
                total += 1
                if same(f, gline.get(f), wline.get(f)):
                    right += 1
                else:
                    misses.append(f"{fname} line {i+1}: {f} = {gline.get(f)!r}, printed {wline.get(f)!r}")
    return {"documents": docs, "fields_checked": total, "fields_right": right,
            "accuracy_pct": round(100 * right / total, 2), "misses": misses}


if __name__ == "__main__":
    import sys
    fp = "--first-pass" in sys.argv
    r = score(first_pass=fp)
    (HERE / "output").mkdir(exist_ok=True)
    name = "accuracy_first_pass.json" if fp else "accuracy.json"
    (HERE / "output" / name).write_text(json.dumps(r, indent=2), encoding="utf-8")
    print(("FIRST PASS: " if fp else "AFTER CHECK: ") + f"{r['fields_right']} of {r['fields_checked']} fields right ({r['accuracy_pct']}%) on {r['documents']} documents")
    for m in r["misses"]:
        print("  MISS", m)
