"""Daily cost anomalies per team and platform.

Each day is compared with the 28 days before it using a robust z-score
(median and median absolute deviation), so one bad day doesn't distort the
baseline. A day is flagged when it is far from normal AND the dollar change
is big enough to matter.

Spend that follows business volume (content delivery follows streaming hours)
is checked per streaming hour instead of in raw dollars. Otherwise a busy
launch weekend looks like a cost problem when it is just more viewing.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .load import Dataset

BASELINE_DAYS = 28
Z_LIMIT = 4.0
MIN_DOLLARS = 1_000
DRIVER_LINKED = {"Networking"}


def robust_z(series: pd.Series, baseline: int = BASELINE_DAYS) -> pd.DataFrame:
    past = series.shift(1).rolling(baseline, min_periods=baseline)
    med = past.median()
    mad = past.apply(lambda w: np.median(np.abs(w - np.median(w))), raw=True)
    # A perfectly flat history has no spread; use 1% of the level as a floor.
    scale = np.maximum(1.4826 * mad, 0.01 * med.abs())
    z = (series - med) / scale
    return pd.DataFrame({"value": series, "baseline": med, "z": z})


def daily(ds: Dataset) -> pd.DataFrame:
    u = ds.bill[ds.bill["ChargeCategory"] == "Usage"].copy()
    u["team"] = u["team"].fillna("untagged")
    d = u.groupby(["team", "ServiceCategory", "date"])["EffectiveCost"].sum().reset_index()
    return d.merge(ds.drivers, on="date", how="left")


def detect(ds: Dataset, normalize: bool = True) -> pd.DataFrame:
    d = daily(ds)
    flags = []
    for (team, cat), g in d.groupby(["team", "ServiceCategory"]):
        g = g.set_index("date").sort_index()
        per_hour = normalize and cat in DRIVER_LINKED
        series = g["EffectiveCost"] / g["streaming_hours"] * 1000 if per_hour else g["EffectiveCost"]
        z = robust_z(series)
        excess = g["EffectiveCost"] - (z["baseline"] * g["streaming_hours"] / 1000 if per_hour else z["baseline"])
        hit = (z["z"].abs() >= Z_LIMIT) & (excess.abs() >= MIN_DOLLARS)
        for day in z.index[hit]:
            flags.append({"date": day.strftime("%Y-%m-%d"), "team": team, "ServiceCategory": cat,
                          "measure": "cost per 1,000 streaming hours" if per_hour else "daily cost",
                          "cost": round(float(g.loc[day, "EffectiveCost"]), 2),
                          "expected": round(float(g.loc[day, "EffectiveCost"] - excess.loc[day]), 2),
                          "excess": round(float(excess.loc[day]), 2), "z": round(float(z.loc[day, "z"]), 1)})
    cols = ["date", "team", "ServiceCategory", "measure", "cost", "expected", "excess", "z"]
    return pd.DataFrame(flags, columns=cols).sort_values(["date", "team"]).reset_index(drop=True)
