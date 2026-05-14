#!/usr/bin/env python3
"""
Extract Caltex Terminal Gate Pricing from PDF into tidy CSVs and JSON.

Outputs:
  tgp-caltex-current.csv  - current effective date prices (overwritten each run)
  tgp-caltex-history.csv  - all dates ever seen (appended, no duplicates)
  tgp_data.json           - full normalised history, regenerated each run
"""

import csv
import json
import os
import re
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone

try:
    import pdfplumber
except ImportError:
    print("Error: pdfplumber not installed. Run: pip install pdfplumber", file=sys.stderr)
    sys.exit(1)

PDF_URL = (
    "https://www.caltex.com/content/dam/caltex/Australia/b2b/products/"
    "terminal-gate-pricing/caltex-terminal-gate-pricing.pdf"
)

CURRENT_CSV = "tgp-caltex-current.csv"
HISTORY_CSV = "tgp-caltex-history.csv"
JSON_OUT = "tgp_data.json"
PROVIDER = "caltex"

FIELDNAMES = ["date", "state", "location", "fuel_type", "price_cpl"]

# Order matches the PDF column pairs (left to right)
FUEL_TYPES = ["e10", "diesel", "p95", "ulp91", "p98", "b5"]

# Map any legacy / PDF-style fuel labels to the canonical short codes
FUEL_ALIASES = {
    "E10": "e10",
    "ULS Diesel": "diesel",
    "Diesel": "diesel",
    "Premium Diesel": "prediesel",
    "PULP 95": "p95",
    "ULP 91": "ulp91",
    "PULP 98": "p98",
    "B5": "b5",
}

# Regex matching a data row: STATE  Location  val val val ... (12 values)
ROW_RE = re.compile(
    r"^(QLD|NSW|VIC|SA|WA|TAS|NT|ACT)\s+"  # state code
    r"(.+?)\s+"                              # location name (non-greedy)
    r"((?:[\d.]+|N/A)(?:\s+(?:[\d.]+|N/A)){11})$",  # exactly 12 price tokens
    re.MULTILINE,
)


def normalise_fuel(fuel: str) -> str:
    return FUEL_ALIASES.get(fuel, fuel)


def parse_ddmmyyyy(date_str: str) -> str:
    """Convert 'dd/mm/yyyy' to ISO 'yyyy-mm-dd'."""
    return datetime.strptime(date_str, "%d/%m/%Y").strftime("%Y-%m-%d")


def download_pdf(url: str) -> str:
    """Download PDF to a temp file and return its path."""
    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    print(f"Downloading {url}", file=sys.stderr)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req) as resp:
        tmp.write(resp.read())
    tmp.close()
    return tmp.name


def extract_text(pdf_path: str) -> str:
    with pdfplumber.open(pdf_path) as pdf:
        return pdf.pages[0].extract_text() or ""


def parse_pricing(text: str):
    """
    Parse all pricing rows from the PDF text.
    Returns (rows, current_date). Rows are dicts matching FIELDNAMES.
    Both current and previous effective dates are included.
    """
    current_match = re.search(r"Current Effective Date\s+\w{3}\s+(\d{2}/\d{2}/\d{4})", text)
    previous_match = re.search(r"Previous Effective Date\s+\w{3}\s+(\d{2}/\d{2}/\d{4})", text)

    if not current_match:
        raise ValueError("Could not find 'Current Effective Date' in PDF text")

    current_date = parse_ddmmyyyy(current_match.group(1))
    previous_date = parse_ddmmyyyy(previous_match.group(1)) if previous_match else None

    rows = []
    for m in ROW_RE.finditer(text):
        state = m.group(1)
        location = m.group(2)
        values = m.group(3).split()  # 12 tokens: prev0 curr0 prev1 curr1 ...

        for i, fuel in enumerate(FUEL_TYPES):
            prev_val = values[i * 2]
            curr_val = values[i * 2 + 1]

            if curr_val != "N/A":
                rows.append({
                    "date": current_date,
                    "state": state,
                    "location": location,
                    "fuel_type": fuel,
                    "price_cpl": round(float(curr_val), 1),
                })

            if prev_val != "N/A" and previous_date:
                rows.append({
                    "date": previous_date,
                    "state": state,
                    "location": location,
                    "fuel_type": fuel,
                    "price_cpl": round(float(prev_val), 1),
                })

    return rows, current_date


def write_current(rows: list[dict], current_date: str) -> None:
    """Overwrite current CSV with only today's prices."""
    current_rows = [r for r in rows if r["date"] == current_date]
    with open(CURRENT_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(current_rows)
    print(f"Wrote {len(current_rows)} rows to {CURRENT_CSV}", file=sys.stderr)


def write_history(rows: list[dict]) -> None:
    """Append rows for any date not already in the history CSV."""
    existing_dates: set[str] = set()
    if os.path.exists(HISTORY_CSV):
        with open(HISTORY_CSV, newline="") as f:
            for row in csv.DictReader(f):
                existing_dates.add(row["date"])

    new_rows = [r for r in rows if r["date"] not in existing_dates]
    if not new_rows:
        print("History up to date — no new dates to append.", file=sys.stderr)
        return

    new_rows.sort(key=lambda r: (r["date"], r["state"], r["location"], r["fuel_type"]))

    needs_header = not os.path.exists(HISTORY_CSV)
    with open(HISTORY_CSV, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        if needs_header:
            writer.writeheader()
        writer.writerows(new_rows)

    added_dates = sorted({r["date"] for r in new_rows})
    print(
        f"Appended {len(new_rows)} rows ({', '.join(added_dates)}) to {HISTORY_CSV}",
        file=sys.stderr,
    )


def write_json() -> None:
    """Read the full history CSV, normalise, and write tgp_data.json from scratch."""
    records = []
    if os.path.exists(HISTORY_CSV):
        with open(HISTORY_CSV, newline="") as f:
            for row in csv.DictReader(f):
                fuel = normalise_fuel(row["fuel_type"])
                try:
                    price = round(float(row["price_cpl"]), 1)
                except (KeyError, ValueError):
                    continue
                records.append([row["date"], row["state"], row["location"], fuel, price])

    records.sort(key=lambda r: (r[0], r[1], r[2], r[3]))

    payload = {
        "provider": PROVIDER,
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "fields": FIELDNAMES,
        "records": records,
    }

    with open(JSON_OUT, "w") as f:
        json.dump(payload, f, separators=(",", ":"))
        f.write("\n")
    print(f"Wrote {len(records)} records to {JSON_OUT}", file=sys.stderr)


def main() -> None:
    pdf_path = download_pdf(PDF_URL)
    try:
        text = extract_text(pdf_path)
        rows, current_date = parse_pricing(text)
        print(f"Parsed {len(rows)} price records (current date: {current_date})", file=sys.stderr)
        write_current(rows, current_date)
        write_history(rows)
        write_json()
    finally:
        os.unlink(pdf_path)


if __name__ == "__main__":
    main()
