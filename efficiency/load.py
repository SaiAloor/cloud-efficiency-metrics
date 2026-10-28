"""Load the billing export and the supporting tables, and run the data checks.

The billing file follows the FinOps FOCUS column names, so a real FOCUS
export from AWS, Azure, Google Cloud or Oracle can be dropped in its place.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

DATA = Path(__file__).resolve().parent.parent / "data"

REQUIRED = [
    "BillingPeriodStart", "ChargePeriodStart", "ChargeCategory", "PricingCategory", "ServiceCategory",
    "ServiceName", "ResourceId", "ResourceType", "ConsumedQuantity", "ConsumedUnit",
    "ListCost", "EffectiveCost", "BilledCost", "Tags",
]
RECON_TOLERANCE = 1.00  # dollars


@dataclass
class Check:
    name: str
    passed: bool
    detail: str
    level: str = "error"   # an error stops the run; a warning is reported

    def as_dict(self):
        return {"check": self.name, "passed": self.passed, "level": self.level, "detail": self.detail}


@dataclass
class Dataset:
    bill: pd.DataFrame
    telemetry: pd.DataFrame
    inventory: pd.DataFrame
    drivers: pd.DataFrame
    ai_usage: pd.DataFrame
    invoices: pd.DataFrame
    checks: list[Check] = field(default_factory=list)
    folder: Path = DATA

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.passed and c.level == "error"]


def _parse_tags(s: str) -> dict:
    try:
        return json.loads(s) if isinstance(s, str) and s else {}
    except json.JSONDecodeError:
        return {}


def check_schema(bill: pd.DataFrame) -> Check:
    missing = [c for c in REQUIRED if c not in bill.columns]
    return Check("FOCUS columns present", not missing,
                 "all required columns found" if not missing else f"missing: {missing}")


def dedupe(bill: pd.DataFrame) -> tuple[pd.DataFrame, Check]:
    """Exact duplicate line items are an export problem, not real spend."""
    dup = bill.duplicated(keep="first")
    n, dollars = int(dup.sum()), float(bill.loc[dup, "BilledCost"].sum())
    detail = "no duplicate line items" if n == 0 else (
        f"removed {n:,} duplicated line items worth ${dollars:,.2f} "
        f"(days: {', '.join(sorted(bill.loc[dup, 'ChargePeriodStart'].unique()))})")
    return bill[~dup].reset_index(drop=True), Check("Duplicate line items", True, detail, "warning")


def check_invoices(bill: pd.DataFrame, invoices: pd.DataFrame) -> list[Check]:
    """Billed cost per month must match the provider's invoice."""
    billed = bill.groupby("BillingPeriodStart")["BilledCost"].sum()
    out = []
    for _, inv in invoices.iterrows():
        got = float(billed.get(inv["BillingPeriodStart"], 0.0))
        gap = got - inv["InvoiceTotal"]
        out.append(Check(f"Invoice match {inv['BillingPeriodStart'][:7]}", abs(gap) <= RECON_TOLERANCE,
                         f"billed ${got:,.2f} vs invoice ${inv['InvoiceTotal']:,.2f} (gap ${gap:+,.2f})"))
    return out


def check_amortization(bill: pd.DataFrame) -> Check:
    """Commitment purchases (billed) should equal the amortized usage (effective)."""
    purchase = bill.loc[bill["ChargeCategory"] == "Purchase", "BilledCost"].sum()
    amortized = bill.loc[(bill["PricingCategory"] == "Commitment-Based") & (bill["ChargeCategory"] == "Usage"),
                         "EffectiveCost"].sum()
    gap = purchase - amortized
    return Check("Commitments fully amortized", abs(gap) <= RECON_TOLERANCE,
                 f"purchases ${purchase:,.2f} vs amortized usage ${amortized:,.2f} (gap ${gap:+,.2f})")


def check_signs(bill: pd.DataFrame) -> Check:
    bad = bill[(bill["EffectiveCost"] < 0) & (bill["ChargeCategory"] != "Credit")]
    return Check("Negative cost only on credits", bad.empty,
                 "ok" if bad.empty else f"{len(bad)} negative usage lines")


def check_periods(bill: pd.DataFrame) -> Check:
    bad = bill[bill["ChargePeriodStart"].str[:7] != bill["BillingPeriodStart"].str[:7]]
    return Check("Charges inside their billing month", bad.empty,
                 "ok" if bad.empty else f"{len(bad)} lines dated outside their billing month")


def check_tags(bill: pd.DataFrame) -> Check:
    usage = bill[bill["ChargeCategory"] == "Usage"]
    tagged = usage["team"].notna()
    share = usage.loc[tagged, "EffectiveCost"].sum() / usage["EffectiveCost"].sum()
    return Check("Team tag coverage", share >= 0.95,
                 f"{share:.1%} of usage cost has a team tag (target 95%)", "warning")


def load(data: Path = DATA) -> Dataset:
    bill = pd.read_csv(data / "billing_focus.csv.gz", dtype={"PricingCategory": str}, keep_default_na=False,
                       na_values={"ConsumedQuantity": [""]})
    checks = [check_schema(bill)]
    if not checks[0].passed:
        return Dataset(bill, *(pd.DataFrame() for _ in range(5)), checks=checks)

    bill, dup_check = dedupe(bill)
    tags = bill["Tags"].map(_parse_tags)
    for key in ("team", "env", "app"):
        bill[key] = tags.map(lambda t: t.get(key))
    bill["date"] = pd.to_datetime(bill["ChargePeriodStart"])
    bill["month"] = bill["BillingPeriodStart"].str[:7]

    ds = Dataset(
        bill=bill,
        telemetry=pd.read_csv(data / "telemetry.csv.gz", parse_dates=["date"]),
        inventory=pd.read_csv(data / "inventory.csv"),
        drivers=pd.read_csv(data / "drivers.csv", parse_dates=["date"]),
        ai_usage=pd.read_csv(data / "ai_usage.csv", parse_dates=["date"]),
        invoices=pd.read_csv(data / "invoices.csv"),
        folder=data,
    )
    ds.checks = checks + [dup_check] + check_invoices(bill, ds.invoices) + [
        check_amortization(bill), check_signs(bill), check_periods(bill), check_tags(bill)]
    return ds
