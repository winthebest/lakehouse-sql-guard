"""Real public dataset for the leakage benchmark: UCI Online Retail II.

Source: Chen, D. (2019). Online Retail II [Dataset]. UCI Machine Learning Repository.
https://doi.org/10.24432/C5CG6D — licensed CC BY 4.0. The raw file is downloaded on demand
into `data/raw/` (git-ignored) and verified by SHA-256; it is never committed.

Transactions are real; the *ingestion* process is simulated, because the dataset has no
load timestamps: invoices are exported in weekly batches (loaded the Monday 00:00 after the
invoice day) and a partner feed for non-UK customers lands one week later. Each batch is one
Iceberg commit, giving a real snapshot history for replay.

Prediction spine: on the first day of each month, every customer seen in the previous 180
days gets a label `churned` = no invoice in the next 90 days.
"""

from __future__ import annotations

import hashlib
import urllib.request
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from sqlguard.core.catalog import LOGICAL_TS_PROPERTY, LakehouseCatalog

URL = "https://archive.ics.uci.edu/static/public/502/online+retail+ii.zip"
SHA256 = "572e36277c2390fbfde10664750731e0a86f55e33470d91919085f0408e67bfb"
RAW_DIR = Path("data/raw")
ZIP_PATH = RAW_DIR / "online_retail_ii.zip"
INVOICES_CACHE = RAW_DIR / "online_retail_ii_invoices.parquet"
SPEC_PATH = Path(__file__).parent / "real_spec.yaml"

HOME_COUNTRY = "United Kingdom"
PARTNER_DELAY = timedelta(days=7)
LOOKBACK = timedelta(days=180)
HORIZON = timedelta(days=90)

SCHEMAS = {
    "customers": pa.schema([("customer_id", pa.int64()), ("country", pa.string())]),
    "invoices": pa.schema(
        [
            ("invoice_id", pa.string()),
            ("customer_id", pa.int64()),
            ("invoice_ts", pa.timestamp("us")),
            ("ingested_at", pa.timestamp("us")),
            ("amount", pa.float64()),
            ("n_lines", pa.int64()),
        ]
    ),
    "churn_labels": pa.schema(
        [("customer_id", pa.int64()), ("cutoff_ts", pa.timestamp("us")), ("churned", pa.int64())]
    ),
    "customer_outcomes": pa.schema(
        [
            ("customer_id", pa.int64()),
            ("cutoff_ts", pa.timestamp("us")),
            ("next_90d_spend", pa.float64()),
        ]
    ),
}


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(path: Path = ZIP_PATH) -> Path:
    """Download the archive if missing and verify its checksum."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(URL, path)  # noqa: S310 - fixed https URL
    actual = sha256_of(path)
    if actual != SHA256:
        raise RuntimeError(f"checksum mismatch for {path}: {actual} != {SHA256}")
    return path


def load_invoices(cache: Path = INVOICES_CACHE) -> pa.Table:
    """Invoice-level rows (cancellations and anonymous customers removed), cached as Parquet."""
    if cache.exists():
        return pq.read_table(cache)
    import openpyxl  # bench extra; only needed for the one-time conversion

    totals: dict[str, dict] = {}
    with zipfile.ZipFile(download()) as archive, archive.open("online_retail_II.xlsx") as f:
        workbook = openpyxl.load_workbook(f, read_only=True)
        for sheet in workbook.worksheets:
            rows = sheet.iter_rows(values_only=True)
            next(rows)  # header
            for invoice, _, _, qty, ts, price, customer, country in rows:
                invoice = str(invoice)
                if customer is None or invoice.startswith("C") or qty is None or price is None:
                    continue
                entry = totals.setdefault(
                    invoice,
                    {
                        "invoice_id": invoice,
                        "customer_id": int(customer),
                        "invoice_ts": ts,
                        "country": country,
                        "amount": 0.0,
                        "n_lines": 0,
                    },
                )
                entry["amount"] += float(qty) * float(price)
                entry["n_lines"] += 1
                entry["invoice_ts"] = min(entry["invoice_ts"], ts)
    rows = sorted(totals.values(), key=lambda r: (r["invoice_ts"], r["invoice_id"]))
    for row in rows:
        row["amount"] = round(row["amount"], 2)
    table = pa.Table.from_pylist(rows)
    cache.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, cache)
    return table


def _next_monday(ts: datetime) -> datetime:
    day = datetime(ts.year, ts.month, ts.day)
    return day + timedelta(days=7 - day.weekday())


def _month_starts(first: datetime, last: datetime) -> list[datetime]:
    out, current = [], datetime(first.year, first.month, 1)
    while current <= last:
        out.append(current)
        current = datetime(current.year + current.month // 12, current.month % 12 + 1, 1)
    return out


def _commit(catalog: LakehouseCatalog, table: str, rows: list[dict], logical: datetime) -> None:
    if rows:
        data = pa.Table.from_pylist(rows, schema=SCHEMAS[table])
        catalog.table(table).append(
            data, snapshot_properties={LOGICAL_TS_PROPERTY: logical.isoformat()}
        )


def build_real_world(catalog: LakehouseCatalog, invoices: pa.Table | None = None) -> dict:
    """Load invoices as weekly Iceberg batches and derive the monthly churn spine."""
    rows = (invoices if invoices is not None else load_invoices()).to_pylist()
    for name, schema in SCHEMAS.items():
        catalog.create_empty(name, schema)

    countries: dict[int, str] = {}
    batches: dict[datetime, list[dict]] = defaultdict(list)
    by_customer: dict[int, list[tuple[datetime, float]]] = defaultdict(list)
    for r in rows:
        countries.setdefault(r["customer_id"], r["country"])
        loaded = _next_monday(r["invoice_ts"])
        if r["country"] != HOME_COUNTRY:
            loaded += PARTNER_DELAY
        batches[loaded].append(
            {
                "invoice_id": r["invoice_id"],
                "customer_id": r["customer_id"],
                "invoice_ts": r["invoice_ts"],
                "ingested_at": loaded,
                "amount": r["amount"],
                "n_lines": r["n_lines"],
            }
        )
        by_customer[r["customer_id"]].append((r["invoice_ts"], r["amount"]))

    first = min(r["invoice_ts"] for r in rows)
    last = max(r["invoice_ts"] for r in rows)
    _commit(
        catalog,
        "customers",
        [{"customer_id": c, "country": k} for c, k in sorted(countries.items())],
        first,
    )
    for loaded in sorted(batches):
        _commit(catalog, "invoices", batches[loaded], loaded)

    labels, outcomes = [], []
    for cutoff in _month_starts(first + LOOKBACK, last - HORIZON):
        for customer, history in sorted(by_customer.items()):
            if not any(cutoff - LOOKBACK <= ts < cutoff for ts, _ in history):
                continue
            future = [amount for ts, amount in history if cutoff <= ts < cutoff + HORIZON]
            labels.append(
                {"customer_id": customer, "cutoff_ts": cutoff, "churned": int(not future)}
            )
            outcomes.append(
                {
                    "customer_id": customer,
                    "cutoff_ts": cutoff,
                    "next_90d_spend": round(sum(future), 2),
                }
            )
    end = last + timedelta(days=1)
    _commit(catalog, "churn_labels", labels, end)
    _commit(catalog, "customer_outcomes", outcomes, end)
    return {
        "invoices": len(rows),
        "customers": len(countries),
        "batches": len(batches),
        "labels": len(labels),
        "churn_rate": round(sum(r["churned"] for r in labels) / len(labels), 4),
        "cutoffs": len({r["cutoff_ts"] for r in labels}),
    }
