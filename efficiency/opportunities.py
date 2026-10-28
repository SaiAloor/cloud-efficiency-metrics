"""Find and size savings opportunities, one resource at a time.

Each rule returns rows of (resource, type, action, monthly cost, monthly savings,
confidence). Savings are per resource and never double counted: a resource is
assigned to the first rule that matches it, in the order below.

Confidence is how likely the saving is to be realized once someone acts on it.
It turns raw savings into a target people can believe.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .load import Dataset

WINDOW_DAYS = 30
IDLE_CPU = 0.05            # average CPU below this, and
IDLE_CPU_PEAK = 0.10       # peak below this, means nobody is using the machine
TARGET_PEAK = 0.65         # size machines so their busiest hour runs near 65%
OVERSIZED_PEAK = 0.40      # peak below this means the machine is too big
SIZES = [2, 4, 8, 16, 32, 64]
GPU_TARGET = 0.70
GPU_LOW = 0.30
STALE_DAYS = 90
STD_RATE, ARCHIVE_RATE = 0.023, 0.004
COMMIT_DISCOUNT = 0.35

CONFIDENCE = {
    "orphaned_volume": 0.9, "idle_compute": 0.9, "stale_storage": 0.8, "commitment_gap": 0.8,
    "oversized_compute": 0.6, "underused_gpu": 0.6,
}
LABELS = {
    "idle_compute": "Shut down idle machines", "orphaned_volume": "Delete unattached disks",
    "stale_storage": "Move unread data to archive", "oversized_compute": "Right-size oversized machines",
    "underused_gpu": "Consolidate underused GPUs", "commitment_gap": "Cover steady usage with commitments",
}


SHORT = {
    "idle_compute": "Idle\nmachines", "orphaned_volume": "Unattached\ndisks", "stale_storage": "Unread\nstorage",
    "oversized_compute": "Oversized\nmachines", "underused_gpu": "Underused\nGPUs", "commitment_gap": "Commitment\ngaps",
}


def window(ds: Dataset) -> tuple[pd.Timestamp, pd.Timestamp]:
    end = ds.bill["date"].max()
    return end - pd.Timedelta(days=WINDOW_DAYS - 1), end


def _row(r, kind, action, savings):
    return {"ResourceId": r.ResourceId, "team": r.team, "ServiceCategory": r.ServiceCategory, "type": kind,
            "action": action, "monthly_cost": r.cost, "monthly_savings": savings,
            "confidence": CONFIDENCE[kind], "expected_savings": savings * CONFIDENCE[kind]}


def find(ds: Dataset) -> pd.DataFrame:
    start, end = window(ds)
    usage = ds.bill[(ds.bill["ChargeCategory"] == "Usage") & ds.bill["date"].between(start, end)]
    days_seen = usage.groupby("ResourceId")["date"].nunique()
    res = (usage.groupby(["ResourceId", "ServiceCategory", "ServiceName", "PricingCategory"], dropna=False)
           .agg(cost=("EffectiveCost", "sum"), team=("team", "first"), env=("env", "first")).reset_index())
    res["team"] = res["team"].fillna("untagged")
    res["days"] = res["ResourceId"].map(days_seen)

    tel = ds.telemetry[ds.telemetry["date"].between(start, end)]
    last14 = tel[tel["date"] > end - pd.Timedelta(days=14)]
    cpu = tel.groupby("ResourceId").agg(cpu_avg=("cpu_avg", "mean"), cpu_peak=("cpu_p95", lambda s: s.quantile(0.95)),
                                        vcpu=("vcpu", "max"), gpu_avg=("gpu_avg", "mean"))
    recent = last14.groupby("ResourceId").agg(avg14=("cpu_avg", "mean"), peak14=("cpu_p95", "max"))
    res = res.join(cpu, on="ResourceId").join(recent, on="ResourceId")
    inv = ds.inventory.set_index("ResourceId")
    res = res.join(inv, on="ResourceId")

    out, taken = [], set()

    def add(df, kind, action, savings_fn):
        for r in df.itertuples():
            if r.ResourceId in taken:
                continue
            s = float(savings_fn(r))
            if s > 0:
                out.append(_row(r, kind, action(r), s))
                taken.add(r.ResourceId)

    vms = res[res["ServiceName"] == "Virtual Machines"]
    add(res[res["attached"] == False], "orphaned_volume", lambda r: "snapshot, then delete",  # noqa: E712
        lambda r: r.cost)
    add(vms[(vms["avg14"] < IDLE_CPU) & (vms["peak14"] < IDLE_CPU_PEAK)], "idle_compute",
        lambda r: f"shut down ({r.env})", lambda r: r.cost)

    stale_cut = (end - pd.Timedelta(days=STALE_DAYS)).strftime("%Y-%m-%d")
    stale = res[(res["storage_tier"] == "standard") & (res["last_read"] < stale_cut)]
    add(stale, "stale_storage", lambda r: f"archive (last read {r.last_read})",
        lambda r: r.cost * (1 - ARCHIVE_RATE / STD_RATE))

    def new_size(r):
        need = r.vcpu * r.cpu_peak / TARGET_PEAK
        return next((s for s in SIZES if s >= need), r.vcpu)

    big = vms[(vms["cpu_peak"] < OVERSIZED_PEAK) & (vms["vcpu"] >= 4)]
    add(big, "oversized_compute", lambda r: f"{int(r.vcpu)} -> {int(new_size(r))} vCPU",
        lambda r: r.cost * (1 - new_size(r) / r.vcpu))

    gpus = res[res["ServiceName"] == "GPU Compute"]

    def gpu_keep(r):
        return max(1, math.ceil(8 * r.gpu_avg / GPU_TARGET))

    add(gpus[gpus["gpu_avg"] < GPU_LOW], "underused_gpu", lambda r: f"8 -> {gpu_keep(r)} GPUs",
        lambda r: r.cost * (1 - gpu_keep(r) / 8))

    steady = vms[(vms["PricingCategory"] == "On-Demand") & (vms["env"] == "prod") & (vms["days"] == WINDOW_DAYS)]
    add(steady, "commitment_gap", lambda r: "move to 1-year commitment", lambda r: r.cost * COMMIT_DISCOUNT)

    cols = ["ResourceId", "team", "ServiceCategory", "type", "action", "monthly_cost", "monthly_savings",
            "confidence", "expected_savings"]
    df = pd.DataFrame(out, columns=cols)
    return df.sort_values("monthly_savings", ascending=False).reset_index(drop=True)


def summarize(opps: pd.DataFrame) -> pd.DataFrame:
    s = (opps.groupby("type").agg(resources=("ResourceId", "count"), monthly_savings=("monthly_savings", "sum"),
                                  expected_savings=("expected_savings", "sum"), confidence=("confidence", "first"))
         .reset_index())
    s["label"] = s["type"].map(LABELS)
    s["short"] = s["type"].map(SHORT)
    return s.sort_values("expected_savings", ascending=False).reset_index(drop=True)


def score_against_planted(opps: pd.DataFrame, planted: pd.DataFrame) -> pd.DataFrame:
    """Precision and recall for each problem type that was hidden in the data."""
    mapping = {"idle_compute": "idle_compute", "oversized_compute": "oversized_compute",
               "underused_gpu": "underused_gpu", "stale_storage": "stale_storage", "orphaned_volume": "orphaned_volume"}
    rows = []
    for kind, issue in mapping.items():
        found = set(opps.loc[opps["type"] == kind, "ResourceId"])
        truth = set(planted.loc[planted["issue"] == issue, "ResourceId"])
        hit = len(found & truth)
        rows.append({"type": LABELS[kind], "planted": len(truth), "flagged": len(found), "correct": hit,
                     "precision": hit / len(found) if found else np.nan,
                     "recall": hit / len(truth) if truth else np.nan})
    return pd.DataFrame(rows)
