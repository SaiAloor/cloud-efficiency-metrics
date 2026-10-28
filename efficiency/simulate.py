"""Simulated cloud bill for a fictional streaming company ("StreamCo").

Real billing data is private, so this module writes a realistic stand-in:

* data/billing_focus.csv.gz   line items in the FinOps FOCUS format
* data/telemetry.csv.gz        daily utilization per compute and GPU resource
* data/inventory.csv           static facts (volume attached?, bucket last read)
* data/drivers.csv             business volume: daily streaming hours
* data/ai_usage.csv            AI tooling usage per team per day
* data/invoices.csv            what the provider says it billed each month
* data/planted_issues.csv      the problems hidden on purpose (answer key)

The planted problems are what a real bill tends to contain: idle and oversized
machines, forgotten storage, unattached disks, underused GPUs, untagged spend,
a runaway batch job, and a duplicated export day. Because we know where they
are, the pipeline can be scored on how many it finds.

Everything is seeded, so the same files come out every time.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

SEED = 42
START, END = "2026-07-01", "2026-09-30"
DATA = Path(__file__).resolve().parent.parent / "data"

TEAMS = {  # team: (app, share of compute fleet)
    "playback": ("playback-api", 0.20),
    "personalization": ("recommendations", 0.16),
    "media": ("encoding-pipeline", 0.18),
    "data": ("data-platform", 0.18),
    "discovery": ("search", 0.10),
    "studio": ("studio-tools", 0.08),
    "eng-productivity": ("dev-tools", 0.10),
}
REGIONS = ["us-east", "us-west", "eu-west", "ap-south"]

VCPU_HOUR = 0.048          # $ per vCPU-hour, on demand
COMMIT_DISCOUNT = 0.35     # commitment-based price is 35% lower
GPU_HOUR = 2.40            # $ per GPU-hour, on demand
STD_GB_MONTH = 0.023       # standard object storage
ARCHIVE_GB_MONTH = 0.004   # archive tier
BLOCK_GB_MONTH = 0.08      # block volumes
CDN_GB = 0.0006            # content delivery, $ per GB
GB_PER_STREAM_HOUR = 2.3
TOKENS_IN, TOKENS_OUT = 3.0, 15.0  # $ per million tokens
DAYS_PER_MONTH = 30.4


def _tags(team=None, env=None, app=None, **extra):
    t = {k: v for k, v in {"team": team, "env": env, "app": app, **extra}.items() if v}
    return json.dumps(t, sort_keys=True)


def simulate(out: Path = DATA, seed: int = SEED) -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    days = pd.date_range(START, END, freq="D")
    n = len(days)
    rows, tele, inventory, planted = [], [], [], []

    def line(day, cat, svc, rid, rtype, region, sku, qty, unit, list_cost, eff, billed, tags,
             charge="Usage", pricing="On-Demand"):
        rows.append({
            "BillingPeriodStart": day.strftime("%Y-%m-01"), "ChargePeriodStart": day.strftime("%Y-%m-%d"),
            "ChargeCategory": charge, "PricingCategory": pricing, "ProviderName": "SimCloud",
            "ServiceCategory": cat, "ServiceName": svc, "ResourceId": rid, "ResourceType": rtype,
            "RegionId": region, "SkuId": sku, "ConsumedQuantity": round(qty, 4), "ConsumedUnit": unit,
            "ListCost": round(list_cost, 4), "EffectiveCost": round(eff, 4), "BilledCost": round(billed, 4),
            "Tags": tags,
        })

    # Business volume: streaming hours with weekly pattern, growth, and a launch on Aug 21.
    dow = np.array([1.0, 0.97, 0.97, 1.0, 1.08, 1.18, 1.15])[days.dayofweek]
    growth = 1 + 0.001 * np.arange(n)
    launch = np.zeros(n)
    li = days.get_loc(pd.Timestamp("2026-08-21"))
    launch[li:li + 4] = [0.35, 0.22, 0.12, 0.05]
    hours = 4_000_000 * dow * growth * (1 + launch) * rng.normal(1, 0.015, n)
    drivers = pd.DataFrame({"date": days.strftime("%Y-%m-%d"), "streaming_hours": hours.round(0).astype(int)})

    # ---- Compute instances -------------------------------------------------
    team_names = list(TEAMS)
    team_p = np.array([TEAMS[t][1] for t in team_names])
    n_vm = 900
    vm_team = rng.choice(team_names, n_vm, p=team_p / team_p.sum())
    vm_env = rng.choice(["prod", "staging", "dev"], n_vm, p=[0.6, 0.2, 0.2])
    vm_vcpu = rng.choice([2, 4, 8, 16, 32, 64], n_vm, p=[0.08, 0.2, 0.3, 0.24, 0.13, 0.05])
    vm_region = rng.choice(REGIONS, n_vm, p=[0.4, 0.25, 0.2, 0.15])
    vm_commit = (vm_env == "prod") & (rng.random(n_vm) < 0.55)
    vm_start = np.where(rng.random(n_vm) < 0.10, rng.integers(5, 60, n_vm), 0)
    vm_end = np.where(rng.random(n_vm) < 0.08, rng.integers(40, n, n_vm), n)
    vm_end = np.maximum(vm_end, vm_start + 20)
    avg = rng.uniform(0.30, 0.62, n_vm)
    p95 = np.minimum(avg + rng.uniform(0.15, 0.30, n_vm), 0.97)
    untagged = rng.random(n_vm) < 0.05

    candidates = np.where((vm_env != "prod") & (vm_end == n))[0]
    idle = rng.choice(candidates, 50, replace=False)
    big = np.where((vm_vcpu >= 16) & (vm_end == n) & ~np.isin(np.arange(n_vm), idle))[0]
    oversized = rng.choice(big, 80, replace=False)
    avg[idle] = rng.uniform(0.005, 0.03, idle.size)
    p95[idle] = avg[idle] + rng.uniform(0.01, 0.03, idle.size)
    avg[oversized] = rng.uniform(0.06, 0.14, oversized.size)
    p95[oversized] = avg[oversized] + rng.uniform(0.05, 0.12, oversized.size)
    # Realistic noise the rules have to cope with:
    # - a few idle machines still run a weekly backup, so they don't look idle on paper;
    # - some healthy prod machines are failover standbys that sit at low load by design.
    weekly_backup = set(rng.choice(idle, 6, replace=False))
    normal = np.where(~np.isin(np.arange(n_vm), np.concatenate([idle, oversized])) & (vm_env == "prod")
                      & (vm_vcpu >= 8) & (vm_end == n))[0]
    standby = rng.choice(normal, 15, replace=False)
    avg[standby] = rng.uniform(0.10, 0.18, standby.size)
    p95[standby] = rng.uniform(0.28, 0.38, standby.size)
    for i in idle:
        planted.append({"ResourceId": f"vm-{i:04d}", "issue": "idle_compute"})
    for i in oversized:
        planted.append({"ResourceId": f"vm-{i:04d}", "issue": "oversized_compute"})
    for i in np.where(untagged)[0]:
        planted.append({"ResourceId": f"vm-{i:04d}", "issue": "untagged"})

    for i in range(n_vm):
        rid = f"vm-{i:04d}"
        tags = _tags(env=vm_env[i]) if untagged[i] else _tags(vm_team[i], vm_env[i], TEAMS[vm_team[i]][0])
        for d in range(vm_start[i], vm_end[i]):
            day = days[d]
            qty = vm_vcpu[i] * 24
            lst = qty * VCPU_HOUR
            if vm_commit[i]:
                eff = lst * (1 - COMMIT_DISCOUNT)
                line(day, "Compute", "Virtual Machines", rid, "Instance", vm_region[i], f"vm-{vm_vcpu[i]}c",
                     qty, "vCPU-Hours", lst, eff, 0.0, tags, pricing="Commitment-Based")
            else:
                line(day, "Compute", "Virtual Machines", rid, "Instance", vm_region[i], f"vm-{vm_vcpu[i]}c",
                     qty, "vCPU-Hours", lst, lst, lst, tags)
            a = float(np.clip(avg[i] * rng.normal(1, 0.08), 0.001, 0.99))
            peak = float(np.clip(p95[i] * rng.normal(1, 0.05), a, 0.995))
            if i in weekly_backup and day.dayofweek == 6:
                peak = float(rng.uniform(0.30, 0.38))
            tele.append({"date": day.strftime("%Y-%m-%d"), "ResourceId": rid, "cpu_avg": round(a, 4),
                         "cpu_p95": round(peak, 4),
                         "gpu_avg": np.nan, "vcpu": int(vm_vcpu[i]), "gpus": np.nan})

    # Runaway backfill job: 40 large on-demand machines left running Aug 14-17.
    for k in range(40):
        rid = f"vm-backfill-{k:02d}"
        planted.append({"ResourceId": rid, "issue": "runaway_job"})
        for day in pd.date_range("2026-08-14", "2026-08-17"):
            qty = 64 * 24
            line(day, "Compute", "Virtual Machines", rid, "Instance", "us-east", "vm-64c", qty, "vCPU-Hours",
                 qty * VCPU_HOUR, qty * VCPU_HOUR, qty * VCPU_HOUR,
                 _tags("data", "prod", "data-platform", job="backfill"))
            tele.append({"date": day.strftime("%Y-%m-%d"), "ResourceId": rid, "cpu_avg": 0.97, "cpu_p95": 0.99,
                         "gpu_avg": np.nan, "vcpu": 64, "gpus": np.nan})

    # ---- GPU nodes -----------------------------------------------------------
    gpu_team = ["personalization"] * 10 + ["eng-productivity"] * 4 + ["media"] * 10
    gpu_util = rng.uniform(0.55, 0.85, 24)
    gpu_idle = rng.choice(24, 5, replace=False)
    gpu_util[gpu_idle] = rng.uniform(0.05, 0.15, 5)
    gpu_commit = rng.random(24) < 0.5
    for j in gpu_idle:
        planted.append({"ResourceId": f"gpu-{j:02d}", "issue": "underused_gpu"})
    for j in range(24):
        rid, tags = f"gpu-{j:02d}", _tags(gpu_team[j], "prod", "ml-platform")
        for day in days:
            qty = 8 * 24
            lst = qty * GPU_HOUR
            eff = lst * (1 - COMMIT_DISCOUNT) if gpu_commit[j] else lst
            line(day, "AI and Machine Learning", "GPU Compute", rid, "GPU Node", "us-east", "gpu-8x", qty,
                 "GPU-Hours", lst, eff, 0.0 if gpu_commit[j] else lst, tags,
                 pricing="Commitment-Based" if gpu_commit[j] else "On-Demand")
            tele.append({"date": day.strftime("%Y-%m-%d"), "ResourceId": rid, "cpu_avg": np.nan, "cpu_p95": np.nan,
                         "gpu_avg": round(float(np.clip(gpu_util[j] * rng.normal(1, 0.06), 0.01, 0.99)), 4),
                         "vcpu": np.nan, "gpus": 8})

    # ---- Object storage ------------------------------------------------------
    n_b = 130
    b_team = rng.choice(team_names, n_b, p=team_p / team_p.sum())
    b_tb = rng.lognormal(3.2, 1.0, n_b)
    b_tier = np.where(rng.random(n_b) < 0.15, "archive", "standard")
    last_read = pd.Timestamp(END) - pd.to_timedelta(rng.integers(0, 30, n_b), unit="D")
    std = np.where(b_tier == "standard")[0]
    stale = rng.choice(std[b_tb[std] > np.median(b_tb)], 18, replace=False)
    last_read = last_read.to_numpy().copy()
    for s in stale:
        last_read[s] = np.datetime64(pd.Timestamp(END) - pd.Timedelta(days=int(rng.integers(120, 400))))
        planted.append({"ResourceId": f"bucket-{s:03d}", "issue": "stale_storage"})
    for b in range(n_b):
        rid = f"bucket-{b:03d}"
        rate = ARCHIVE_GB_MONTH if b_tier[b] == "archive" else STD_GB_MONTH
        inventory.append({"ResourceId": rid, "storage_tier": b_tier[b],
                          "last_read": pd.Timestamp(last_read[b]).strftime("%Y-%m-%d"), "attached": np.nan})
        for d, day in enumerate(days):
            gb = b_tb[b] * 1000 * (1 + 0.0008 * d)
            cost = gb * rate / DAYS_PER_MONTH
            line(day, "Storage", "Object Storage", rid, "Bucket", rng.choice(REGIONS[:2]), f"obj-{b_tier[b]}",
                 gb, "GB-Month", cost, cost, cost, _tags(b_team[b], "prod", TEAMS[b_team[b]][0]))

    # ---- Block volumes -------------------------------------------------------
    n_v = 80
    v_team = rng.choice(team_names, n_v, p=team_p / team_p.sum())
    v_gb = rng.integers(100, 4000, n_v)
    orphan = rng.choice(n_v, 14, replace=False)
    for v in range(n_v):
        rid = f"vol-{v:03d}"
        attached = v not in orphan
        if not attached:
            planted.append({"ResourceId": rid, "issue": "orphaned_volume"})
        inventory.append({"ResourceId": rid, "storage_tier": "block", "last_read": "", "attached": attached})
        for day in days:
            cost = v_gb[v] * BLOCK_GB_MONTH / DAYS_PER_MONTH
            line(day, "Storage", "Block Storage", rid, "Volume", "us-east", "block-ssd", float(v_gb[v]),
                 "GB-Month", cost, cost, cost, _tags(v_team[v], "prod", TEAMS[v_team[v]][0]))

    # ---- Content delivery (scales with streaming hours) -------------------------
    region_share = {"us-east": 0.32, "us-west": 0.22, "eu-west": 0.28, "ap-south": 0.18}
    for d, day in enumerate(days):
        for r, share in region_share.items():
            gb = hours[d] * share * GB_PER_STREAM_HOUR * rng.normal(1, 0.01)
            cost = gb * CDN_GB
            line(day, "Networking", "Content Delivery", f"cdn-{r}", "Distribution", r, "cdn-egress", gb, "GB",
                 cost, cost, cost, _tags("playback", "prod", "playback-api"))

    # ---- AI tooling (generative AI API) ---------------------------------------
    ai_rows = []
    ai_profile = {  # team: (active users, requests per user per day, acceptance rate)
        "eng-productivity": (60, 40, 0.52), "playback": (45, 28, 0.41), "personalization": (40, 30, 0.47),
        "media": (35, 18, 0.33), "data": (50, 34, 0.44), "discovery": (25, 22, 0.38), "studio": (20, 12, 0.29),
    }
    for d, day in enumerate(days):
        weekend = day.dayofweek >= 5
        for team, (users, rpu, acc) in ai_profile.items():
            ramp = 1 + 0.004 * d
            active = int(users * (0.25 if weekend else 1) * ramp * rng.normal(1, 0.05))
            req = int(active * rpu * rng.normal(1, 0.08))
            tin, tout = req * 6000 / 1e6, req * 900 / 1e6
            cost = tin * TOKENS_IN + tout * TOKENS_OUT
            accepted = int(req * float(np.clip(acc * rng.normal(1, 0.05), 0, 1)))
            ai_rows.append({"date": day.strftime("%Y-%m-%d"), "team": team, "active_users": active, "requests": req,
                            "input_mtokens": round(tin, 3), "output_mtokens": round(tout, 3),
                            "accepted_outputs": accepted})
            line(day, "AI and Machine Learning", "Generative AI API", f"genai-{team}", "API Key", "us-east",
                 "llm-std", tin + tout, "Million Tokens", cost, cost, cost, _tags(team, "prod", "ai-assist"))

    bill = pd.DataFrame(rows)

    # Commitment purchases: billed once a month, equal to the amortized effective cost.
    commit = bill[bill["PricingCategory"] == "Commitment-Based"]
    for period, amt in commit.groupby("BillingPeriodStart")["EffectiveCost"].sum().items():
        rows.append({"BillingPeriodStart": period, "ChargePeriodStart": period, "ChargeCategory": "Purchase",
                     "PricingCategory": "Commitment-Based", "ProviderName": "SimCloud", "ServiceCategory": "Compute",
                     "ServiceName": "Savings Plan", "ResourceId": "commitment-plan", "ResourceType": "Commitment",
                     "RegionId": "global", "SkuId": "commit-1yr", "ConsumedQuantity": 0, "ConsumedUnit": "",
                     "ListCost": 0.0, "EffectiveCost": 0.0, "BilledCost": round(amt, 4), "Tags": _tags()})
    # A monthly service credit.
    for period in sorted(bill["BillingPeriodStart"].unique()):
        rows.append({"BillingPeriodStart": period, "ChargePeriodStart": period, "ChargeCategory": "Credit",
                     "PricingCategory": "", "ProviderName": "SimCloud", "ServiceCategory": "Compute",
                     "ServiceName": "Virtual Machines", "ResourceId": "credit", "ResourceType": "Credit",
                     "RegionId": "global", "SkuId": "credit", "ConsumedQuantity": 0, "ConsumedUnit": "",
                     "ListCost": 0.0, "EffectiveCost": -15000.0, "BilledCost": -15000.0, "Tags": _tags()})
    bill = pd.DataFrame(rows)
    invoices = bill.groupby("BillingPeriodStart", as_index=False)["BilledCost"].sum().round(2)
    invoices.columns = ["BillingPeriodStart", "InvoiceTotal"]

    # Export glitch: 300 line items from Sep 12 delivered twice.
    dup = bill[(bill["ChargePeriodStart"] == "2026-09-12") & (bill["ChargeCategory"] == "Usage")].sample(
        300, random_state=seed)
    bill = pd.concat([bill, dup], ignore_index=True)
    planted.append({"ResourceId": "export-2026-09-12", "issue": "duplicate_export"})

    out.mkdir(exist_ok=True)
    files = {
        "billing_focus.csv.gz": bill, "telemetry.csv.gz": pd.DataFrame(tele), "inventory.csv": pd.DataFrame(inventory),
        "drivers.csv": drivers, "ai_usage.csv": pd.DataFrame(ai_rows), "invoices.csv": invoices,
        "planted_issues.csv": pd.DataFrame(planted),
    }
    for name, df in files.items():
        df.to_csv(out / name, index=False)
    return files


if __name__ == "__main__":
    f = simulate()
    b = f["billing_focus.csv.gz"]
    print(f"{len(b):,} billing rows, effective cost ${b['EffectiveCost'].sum():,.0f}")
    print(b.groupby("ServiceCategory")["EffectiveCost"].sum().round(0))
