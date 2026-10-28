"""The metric tree: resource -> team -> platform -> company.

Two rules keep the levels consistent with each other:

1. Dollar metrics (cost, identified waste) are sums, so every level adds up
   exactly to the one above it.
2. Ratio metrics (efficiency, utilization, unit cost) are always a ratio of
   sums, never an average of ratios. Averaging team percentages would let a
   tiny team count as much as a huge one, and the company number would stop
   matching its parts.
"""
from __future__ import annotations

import pandas as pd

from .load import Dataset
from .opportunities import window

PLATFORMS = ["Compute", "AI and Machine Learning", "Storage", "Networking"]
# Services that at least one savings rule can inspect. Efficiency elsewhere is
# "no waste found yet", not "no waste", so coverage is reported next to it.
RULE_COVERED = {"Virtual Machines", "GPU Compute", "Object Storage", "Block Storage"}


def usage_in(ds: Dataset, start, end) -> pd.DataFrame:
    u = ds.bill[(ds.bill["ChargeCategory"] == "Usage") & ds.bill["date"].between(start, end)].copy()
    u["team"] = u["team"].fillna("untagged")
    return u


def _weighted_util(u: pd.DataFrame, tel: pd.DataFrame, col: str) -> float:
    """Utilization weighted by what each resource costs, so it ladders like dollars."""
    per = tel.groupby("ResourceId")[col].mean().dropna()
    cost = u.groupby("ResourceId")["EffectiveCost"].sum()
    both = pd.concat([per, cost], axis=1, join="inner")
    return float((both[col] * both["EffectiveCost"]).sum() / both["EffectiveCost"].sum()) if len(both) else float("nan")


def tree(ds: Dataset, opps: pd.DataFrame) -> dict[str, pd.DataFrame]:
    start, end = window(ds)
    u = usage_in(ds, start, end)
    tel = ds.telemetry[ds.telemetry["date"].between(start, end)]
    hours = ds.drivers.loc[ds.drivers["date"].between(start, end), "streaming_hours"].sum()

    waste_team = opps.groupby("team")["monthly_savings"].sum()
    waste_plat = opps.groupby("ServiceCategory")["monthly_savings"].sum()
    expected_team = opps.groupby("team")["expected_savings"].sum()
    expected_plat = opps.groupby("ServiceCategory")["expected_savings"].sum()

    def level(key, waste, expected):
        df = u.groupby(key, as_index=False)["EffectiveCost"].sum().rename(columns={"EffectiveCost": "cost"})
        df["waste"] = df[key].map(waste).fillna(0.0)
        df["expected_savings"] = df[key].map(expected).fillna(0.0)
        df["efficiency"] = 1 - df["waste"] / df["cost"]
        df["share_of_cost"] = df["cost"] / df["cost"].sum()
        return df.sort_values("cost", ascending=False).reset_index(drop=True)

    teams = level("team", waste_team, expected_team)
    platforms = level("ServiceCategory", waste_plat, expected_plat)
    covered = u[u["ServiceName"].isin(RULE_COVERED)].groupby("ServiceCategory")["EffectiveCost"].sum()
    platforms["rule_coverage"] = platforms["ServiceCategory"].map(covered).fillna(0.0) / platforms["cost"]

    vm = u[u["ServiceName"] == "Virtual Machines"]
    gpu = u[u["ServiceName"] == "GPU Compute"]
    obj = u[u["ServiceName"] == "Object Storage"]
    cdn = u[u["ServiceName"] == "Content Delivery"]
    genai = u[u["ServiceName"] == "Generative AI API"]
    ai = ds.ai_usage[ds.ai_usage["date"].between(start, end)]
    archive = obj[obj["SkuId"] == "obj-archive"]

    platform_units = {
        "Compute": [
            ("Cost-weighted CPU utilization", f"{_weighted_util(vm, tel, 'cpu_avg'):.1%}"),
            ("Effective cost per vCPU-hour", f"${vm['EffectiveCost'].sum() / vm['ConsumedQuantity'].sum():.4f}"),
            ("Commitment coverage (prod vCPU-hours)",
             f"{vm.loc[(vm['env'] == 'prod') & (vm['PricingCategory'] == 'Commitment-Based'), 'ConsumedQuantity'].sum() / vm.loc[vm['env'] == 'prod', 'ConsumedQuantity'].sum():.1%}"),
        ],
        "AI and Machine Learning": [
            ("Cost-weighted GPU utilization", f"{_weighted_util(gpu, tel, 'gpu_avg'):.1%}"),
            ("AI tooling cost per accepted output", f"${genai['EffectiveCost'].sum() / ai['accepted_outputs'].sum():.4f}"),
            ("AI tooling acceptance rate", f"{ai['accepted_outputs'].sum() / ai['requests'].sum():.1%}"),
        ],
        "Storage": [
            ("Object storage cost per TB-month", f"${obj['EffectiveCost'].sum() / (obj['ConsumedQuantity'].sum() / 1000 / 30):,.2f}"),
            ("Share of data in archive tier", f"{archive['ConsumedQuantity'].sum() / obj['ConsumedQuantity'].sum():.1%}"),
        ],
        "Networking": [
            ("Delivery cost per 1,000 streaming hours", f"${cdn['EffectiveCost'].sum() / hours * 1000:.3f}"),
            ("Delivery cost per TB", f"${cdn['EffectiveCost'].sum() / (cdn['ConsumedQuantity'].sum() / 1000):.2f}"),
        ],
    }

    total = float(u["EffectiveCost"].sum())
    waste = float(opps["monthly_savings"].sum())
    expected = float(opps["expected_savings"].sum())
    tagged = u.loc[u["team"] != "untagged", "EffectiveCost"].sum() / total
    company = {
        "window": f"{start:%b %-d} to {end:%b %-d, %Y}",
        "cost": total, "waste": waste, "expected_savings": expected,
        "efficiency": 1 - waste / total,
        "target_efficiency": 1 - (waste - expected) / (total - expected),
        "target_cost": total - expected,
        "cost_per_1k_hours": total / hours * 1000,
        "target_cost_per_1k_hours": (total - expected) / hours * 1000,
        "tag_coverage": tagged,
        "rule_coverage": float(u.loc[u["ServiceName"].isin(RULE_COVERED), "EffectiveCost"].sum() / total),
        "streaming_hours": float(hours),
    }
    return {"company": company, "platforms": platforms, "teams": teams, "platform_units": platform_units}


def monthly(ds: Dataset) -> pd.DataFrame:
    """Cost by platform and unit cost per month, for the trend chart."""
    u = ds.bill[ds.bill["ChargeCategory"] == "Usage"]
    m = u.pivot_table(index="month", columns="ServiceCategory", values="EffectiveCost", aggfunc="sum").fillna(0)
    m = m[[p for p in PLATFORMS if p in m.columns]]
    d = ds.drivers.assign(month=ds.drivers["date"].dt.strftime("%Y-%m")).groupby("month")["streaming_hours"].sum()
    m["total"] = m[[p for p in PLATFORMS if p in m.columns]].sum(axis=1)
    m["streaming_hours"] = d
    m["cost_per_1k_hours"] = m["total"] / m["streaming_hours"] * 1000
    return m.reset_index()


def ai_productivity(ds: Dataset) -> pd.DataFrame:
    """AI tooling: what it costs, how much it is used, how often its output is kept."""
    start, end = window(ds)
    ai = ds.ai_usage[ds.ai_usage["date"].between(start, end)]
    spend = usage_in(ds, start, end)
    spend = spend[spend["ServiceName"] == "Generative AI API"].groupby("team")["EffectiveCost"].sum()
    t = ai.groupby("team").agg(requests=("requests", "sum"), accepted=("accepted_outputs", "sum"),
                               weekday_users=("active_users", lambda s: s[ai.loc[s.index, "date"].dt.dayofweek < 5].mean()))
    t["spend"] = spend
    t["acceptance_rate"] = t["accepted"] / t["requests"]
    t["cost_per_accepted"] = t["spend"] / t["accepted"]
    t["spend_per_user"] = t["spend"] / t["weekday_users"]
    return t.sort_values("spend", ascending=False).reset_index()


def team_by_platform(ds: Dataset) -> pd.DataFrame:
    """Cost for every team and platform pair. Rows and columns both add up to the company total."""
    start, end = window(ds)
    u = usage_in(ds, start, end)
    m = u.pivot_table(index="team", columns="ServiceCategory", values="EffectiveCost", aggfunc="sum", fill_value=0.0)
    m = m[[p for p in PLATFORMS if p in m.columns]]
    m["Total"] = m.sum(axis=1)
    m = m.sort_values("Total", ascending=False)
    m.loc["Total"] = m.sum()
    return m.round(2)
