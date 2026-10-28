"""Charts, result tables, the README results block and the dashboard page."""
from __future__ import annotations

import html
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from .anomalies import robust_z  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT, DOCS, README = ROOT / "outputs", ROOT / "docs", ROOT / "README.md"
START, END = "<!-- RESULTS:START -->", "<!-- RESULTS:END -->"
INK, MUTED, GRID = "#1f2430", "#6b7280", "#e5e7eb"
PLAT_COLORS = {"Compute": "#2563a8", "AI and Machine Learning": "#7c3aed", "Storage": "#0f8a6a", "Networking": "#c2410c"}
SAVE, TARGET = "#0f8a6a", "#1f2430"


def money(x, digits=0):
    return f"${x:,.{digits}f}"


def _style(ax):
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.tick_params(colors=MUTED, labelsize=9)


def chart_waterfall(company, summary, path):
    labels = ["Current\nmonthly cost"] + list(summary["short"]) + ["Target\nmonthly cost"]
    steps = list(summary["expected_savings"])
    fig, ax = plt.subplots(figsize=(10, 4.6))
    level = company["cost"]
    ax.bar(0, level, color="#94a3b8", width=0.6)
    ax.text(0, level, money(level / 1000, 0) + "K", ha="center", va="bottom", fontsize=9, color=INK)
    for i, s in enumerate(steps, start=1):
        ax.bar(i, s, bottom=level - s, color=SAVE, width=0.6)
        ax.text(i, level, f"-{money(s / 1000, 1)}K", ha="center", va="bottom", fontsize=9, color=SAVE)
        level -= s
    ax.bar(len(labels) - 1, level, color=TARGET, width=0.6)
    ax.text(len(labels) - 1, level, money(level / 1000, 0) + "K", ha="center", va="bottom", fontsize=9, color=INK)
    lo = level * 0.85
    ax.set_ylim(lo, company["cost"] * 1.03)
    ax.set_xticks(range(len(labels)), labels, fontsize=8)
    ax.yaxis.set_major_formatter(lambda v, _: f"${v / 1000:,.0f}K")
    ax.set_title("Monthly cost to target, expected savings after confidence weighting", loc="left", fontsize=11, color=INK)
    _style(ax)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def chart_trend(monthly, path):
    fig, ax = plt.subplots(figsize=(10, 4.2))
    bottom = pd.Series(0.0, index=monthly.index)
    plats = [p for p in PLAT_COLORS if p in monthly.columns]
    for p in plats:
        ax.bar(monthly["month"], monthly[p] / 1000, bottom=bottom / 1000, color=PLAT_COLORS[p], width=0.55, label=p)
        bottom += monthly[p]
    ax.yaxis.set_major_formatter(lambda v, _: f"${v:,.0f}K")
    ax2 = ax.twinx()
    ax2.plot(monthly["month"], monthly["cost_per_1k_hours"], color=INK, marker="o", linewidth=2)
    for x, y in zip(monthly["month"], monthly["cost_per_1k_hours"]):
        ax2.annotate(f"${y:.2f}", (x, y), xytext=(0, 8), textcoords="offset points", ha="center", fontsize=9)
    ax2.set_ylim(monthly["cost_per_1k_hours"].min() * 0.9, monthly["cost_per_1k_hours"].max() * 1.08)
    ax2.set_ylabel("Cost per 1,000 streaming hours", color=MUTED, fontsize=9)
    ax2.spines[["top"]].set_visible(False)
    ax2.tick_params(colors=MUTED, labelsize=9)
    ax.set_title("Monthly cost by platform (bars) and unit cost (line)", loc="left", fontsize=11, color=INK)
    ax.legend(frameon=False, fontsize=8, ncol=4, loc="upper left", bbox_to_anchor=(0, -0.08))
    _style(ax)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def chart_anomalies(daily, raw_flags, flags, path):
    fig, axes = plt.subplots(1, 2, figsize=(11, 4))
    g = daily[(daily["team"] == "data") & (daily["ServiceCategory"] == "Compute")].set_index("date")
    ax = axes[0]
    ax.plot(g.index, g["EffectiveCost"], color="#2563a8", linewidth=1.8)
    hit = flags[(flags["team"] == "data") & (flags["ServiceCategory"] == "Compute")]
    hd = pd.to_datetime(hit["date"])
    ax.scatter(hd, g.loc[hd, "EffectiveCost"], color="#dc2626", zorder=3, s=30, label="Flagged")
    ax.set_title("Data team compute: runaway job caught", loc="left", fontsize=10, color=INK)
    ax.yaxis.set_major_formatter(lambda v, _: f"${v:,.0f}")
    ax.legend(frameon=False, fontsize=8)

    c = daily[(daily["team"] == "playback") & (daily["ServiceCategory"] == "Networking")].set_index("date")
    ax = axes[1]
    ax.plot(c.index, c["EffectiveCost"], color="#c2410c", linewidth=1.8, label="Daily cost")
    rh = pd.to_datetime(raw_flags.loc[raw_flags["ServiceCategory"] == "Networking", "date"])
    ax.scatter(rh, c.loc[rh, "EffectiveCost"], color="#dc2626", marker="x", s=40, zorder=3,
               label="Raw-dollar alert (false alarm)")
    ax.yaxis.set_major_formatter(lambda v, _: f"${v:,.0f}")
    ax3 = ax.twinx()
    ax3.plot(c.index, c["EffectiveCost"] / c["streaming_hours"] * 1000, color=INK, linewidth=1.2, linestyle="--",
             label="Per 1,000 streaming hours")
    ax3.set_ylim(0, (c["EffectiveCost"] / c["streaming_hours"] * 1000).max() * 1.6)
    ax3.tick_params(colors=MUTED, labelsize=8)
    ax3.spines[["top"]].set_visible(False)
    ax.set_title("Content delivery: launch weekend is volume, not waste", loc="left", fontsize=10, color=INK)
    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = ax3.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, frameon=False, fontsize=8, loc="upper left")
    for a in axes:
        _style(a)
        a.tick_params(axis="x", rotation=30)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def chart_teams(teams, path):
    t = teams.sort_values("cost")
    fig, ax = plt.subplots(figsize=(10, 4.2))
    ax.barh(t["team"], (t["cost"] - t["waste"]) / 1000, color="#94a3b8", label="Cost with no savings found")
    ax.barh(t["team"], t["waste"] / 1000, left=(t["cost"] - t["waste"]) / 1000, color="#dc2626", label="Identified savings")
    for y, (c, e) in enumerate(zip(t["cost"], t["efficiency"])):
        ax.text(c / 1000 + 3, y, f"{e:.0%} efficient", va="center", fontsize=8, color=INK)
    ax.xaxis.set_major_formatter(lambda v, _: f"${v:,.0f}K")
    ax.set_xlim(0, t["cost"].max() / 1000 * 1.2)
    ax.set_title("Monthly cost and identified savings by team", loc="left", fontsize=11, color=INK)
    ax.legend(frameon=False, fontsize=8, loc="lower right")
    _style(ax)
    ax.grid(axis="x", color=GRID)
    ax.grid(axis="y", visible=False)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def _md(df, cols, fmt=None):
    fmt = fmt or {}
    head = "| " + " | ".join(cols.values()) + " |"
    sep = "| " + " | ".join("---:" if k in fmt else "---" for k in cols) + " |"
    body = ["| " + " | ".join(fmt[k](r[k]) if k in fmt else str(r[k]) for k in cols) + " |" for _, r in df.iterrows()]
    return "\n".join([head, sep, *body])


def mermaid_tree(company, platforms):
    lines = ["```mermaid", "flowchart TD",
             f'    C["<b>Company</b><br/>{money(company["cost"] / 1000)}K a month<br/>'
             f'{company["efficiency"]:.1%} efficient<br/>${company["cost_per_1k_hours"]:.2f} per 1,000 hours"]']
    for i, r in platforms.iterrows():
        eff = "no savings rule yet" if r.get("rule_coverage", 1) == 0 else f'{r["efficiency"]:.1%} efficient'
        lines.append(f'    C --> P{i}["<b>{r["ServiceCategory"]}</b><br/>{money(r["cost"] / 1000)}K<br/>{eff}"]')
    lines.append('    P0 --> T["<b>Team</b><br/>cost, savings, efficiency"]')
    lines.append('    T --> R["<b>Resource</b><br/>cost, utilization, rule matched"]')
    lines.append("```")
    return "\n".join(lines)


def build(ds, company, platforms, teams, units, summary, opps, score, monthly, ai, raw_flags, flags, daily, matrix):
    OUT.mkdir(exist_ok=True)
    DOCS.mkdir(exist_ok=True)
    opps.to_csv(OUT / "opportunities.csv", index=False)
    summary.to_csv(OUT / "opportunity_summary.csv", index=False)
    score.to_csv(OUT / "detection_scorecard.csv", index=False)
    teams.to_csv(OUT / "team_metrics.csv", index=False)
    platforms.to_csv(OUT / "platform_metrics.csv", index=False)
    matrix.to_csv(OUT / "team_by_platform.csv")
    monthly.to_csv(OUT / "monthly.csv", index=False)
    ai.to_csv(OUT / "ai_tooling.csv", index=False)
    flags.to_csv(OUT / "anomalies.csv", index=False)
    (OUT / "data_checks.json").write_text(json.dumps([c.as_dict() for c in ds.checks], indent=2, default=bool) + "\n")

    charts = {"waterfall.png": lambda p: chart_waterfall(company, summary, p),
              "trend.png": lambda p: chart_trend(monthly, p),
              "anomalies.png": lambda p: chart_anomalies(daily, raw_flags, flags, p),
              "teams.png": lambda p: chart_teams(teams, p)}
    sample = DOCS / "sample"
    sample.mkdir(exist_ok=True)
    for name in ("billing_focus.csv.gz", "telemetry.csv.gz", "inventory.csv", "drivers.csv", "invoices.csv"):
        src = ds_dir(ds) / name
        if src.exists():
            (sample / name).write_bytes(src.read_bytes())
    for name, fn in charts.items():
        fn(OUT / name)
        (DOCS / name).write_bytes((OUT / name).read_bytes())

    pct = lambda v: f"{v:.1%}"  # noqa: E731
    k = lambda v: money(v)  # noqa: E731
    dq_ok = sum(1 for c in ds.checks if c.passed)
    first, last = monthly.iloc[0], monthly.iloc[-1]
    kpis = pd.DataFrame([
        {"Metric": "Monthly infrastructure cost", "Now": k(company["cost"]), "Target": k(company["target_cost"])},
        {"Metric": "Cost per 1,000 streaming hours", "Now": f"${company['cost_per_1k_hours']:.2f}",
         "Target": f"${company['target_cost_per_1k_hours']:.2f}"},
        {"Metric": "Efficiency (1 - identified savings / cost)", "Now": pct(company["efficiency"]),
         "Target": pct(company["target_efficiency"])},
        {"Metric": "Team tag coverage", "Now": pct(company["tag_coverage"]), "Target": "95.0% or higher"},
        {"Metric": "Cost covered by a savings rule", "Now": pct(company["rule_coverage"]), "Target": "Add rules for delivery and AI tooling"},
    ])
    block = "\n\n".join([
        f"Window: **{company['window']}**. Data checks: **{dq_ok} of {len(ds.checks)} passed**. "
        f"Unit cost moved from ${first['cost_per_1k_hours']:.2f} in {first['month']} to "
        f"${last['cost_per_1k_hours']:.2f} in {last['month']} per 1,000 streaming hours.",
        _md(kpis, {"Metric": "Metric", "Now": "Now", "Target": "Target"}),
        "![Cost to target](outputs/waterfall.png)",
        _md(summary, {"label": "Opportunity", "resources": "Resources", "monthly_savings": "Monthly savings",
                      "confidence": "Confidence", "expected_savings": "Expected savings"},
            {"resources": str, "monthly_savings": k, "confidence": lambda v: f"{v:.0%}", "expected_savings": k}),
        "**By platform**",
        _md(platforms, {"ServiceCategory": "Platform", "cost": "Monthly cost", "waste": "Identified savings",
                        "efficiency": "Efficiency", "rule_coverage": "Rule coverage"},
            {"cost": k, "waste": k, "efficiency": pct, "rule_coverage": pct}),
        "**How well the rules find the planted problems**",
        _md(score, {"type": "Problem", "planted": "Planted", "flagged": "Flagged", "correct": "Correct",
                    "precision": "Precision", "recall": "Recall"},
            {"planted": str, "flagged": str, "correct": str, "precision": pct, "recall": pct}),
        "![Trend](outputs/trend.png)",
        "![Teams](outputs/teams.png)",
        "![Anomalies](outputs/anomalies.png)",
    ])
    text = README.read_text()
    if START in text and END in text:
        head, rest = text.split(START, 1)
        _, tail = rest.split(END, 1)
        text = f"{head}{START}\n{block}\n{END}{tail}"
    text = _replace_between(text, "<!-- TREE:START -->", "<!-- TREE:END -->", mermaid_tree(company, platforms))
    text = _replace_between(text, "<!-- AI:START -->", "<!-- AI:END -->", _md(
        ai, {"team": "Team", "spend": "Monthly spend", "weekday_users": "Weekday users", "requests": "Requests",
             "acceptance_rate": "Acceptance rate", "cost_per_accepted": "Cost per accepted output"},
        {"spend": k, "weekday_users": lambda v: f"{v:.0f}", "requests": lambda v: f"{v:,}",
         "acceptance_rate": pct, "cost_per_accepted": lambda v: f"${v:.3f}"}))
    README.write_text(text)
    build_site(ds, company, platforms, teams, units, summary, score, ai, flags, kpis)


def ds_dir(ds):
    return getattr(ds, "folder", ROOT / "data")


def _replace_between(text, a, b, new):
    if a not in text or b not in text:
        return text
    head, rest = text.split(a, 1)
    _, tail = rest.split(b, 1)
    return f"{head}{a}\n{new}\n{b}{tail}"


def _html_table(df, cols, fmt=None):
    fmt = fmt or {}
    th = "".join(f"<th{' class=n' if c in fmt else ''}>{html.escape(h)}</th>" for c, h in cols.items())
    rows = "".join("<tr>" + "".join(
        f"<td{' class=n' if c in fmt else ''}>{html.escape(fmt[c](r[c]) if c in fmt else str(r[c]))}</td>"
        for c in cols) + "</tr>" for _, r in df.iterrows())
    return f"<div class=scroll><table><thead><tr>{th}</tr></thead><tbody>{rows}</tbody></table></div>"


def build_site(ds, company, platforms, teams, units, summary, score, ai, flags, kpis):
    k = lambda v: money(v)  # noqa: E731
    pct = lambda v: f"{v:.1%}"  # noqa: E731
    tiles = [
        ("Monthly cost", k(company["cost"]), company["window"]),
        ("Efficiency", pct(company["efficiency"]), f"Target {pct(company['target_efficiency'])}"),
        ("Cost per 1,000 streaming hours", f"${company['cost_per_1k_hours']:.2f}",
         f"Target ${company['target_cost_per_1k_hours']:.2f}"),
        ("Expected monthly savings", k(company["expected_savings"]), f"of {k(company['waste'])} identified"),
        ("Tag coverage", pct(company["tag_coverage"]), "Share of cost with a team owner"),
    ]
    tile_html = "".join(f"<div class=tile><span class=l>{html.escape(a)}</span><span class=big>{html.escape(b)}</span>"
                        f"<span class=l>{html.escape(c)}</span></div>" for a, b, c in tiles)
    unit_rows = "".join(f"<tr><td>{html.escape(p)}</td><td>{html.escape(m)}</td><td class=n>{html.escape(v)}</td></tr>"
                        for p, ms in units.items() for m, v in ms)
    checks = "".join(f"<li class={'ok' if c.passed else 'bad'}>{html.escape(c.name)}: {html.escape(c.detail)}</li>"
                     for c in ds.checks)
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cloud Efficiency Metrics</title>
<style>
:root{{--bg:#f7f8fa;--fg:#1f2430;--muted:#6b7280;--line:#e5e7eb;--panel:#fff;--ok:#0f8a6a;--bad:#dc2626}}
@media (prefers-color-scheme:dark){{:root{{--bg:#14161b;--fg:#e7e9ee;--muted:#9aa1ad;--line:#2a2e37;--panel:#1b1e25}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}}
main{{max-width:1080px;margin:0 auto;padding:32px max(16px,env(safe-area-inset-left))}}
h1{{font-size:28px;margin:0 0 4px}} h2{{font-size:17px;margin:34px 0 8px}}
.lead{{color:var(--muted);margin:0 0 20px;max-width:75ch}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}}
.tile{{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:14px 16px;display:grid;gap:2px}}
.l{{font-size:12px;color:var(--muted)}} .big{{font-size:24px;font-variant-numeric:tabular-nums}}
img{{max-width:100%;background:#fff;border:1px solid var(--line);border-radius:8px}}
.scroll{{overflow-x:auto}} table{{border-collapse:collapse;width:100%;font-size:14px;font-variant-numeric:tabular-nums}}
th,td{{text-align:left;padding:6px 10px;border-bottom:1px solid var(--line)}} th{{color:var(--muted);font-weight:500;font-size:12px}}
.n{{text-align:right}} li.ok{{color:var(--ok)}} li.bad{{color:var(--bad)}}
.note{{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px 14px;color:var(--muted);font-size:13px}}
footer{{color:var(--muted);font-size:13px;margin-top:40px}}
</style></head><body><main>
<h1>Cloud Efficiency Metrics</h1>
<p class=lead>A metric framework for cloud and AI infrastructure cost at a fictional streaming company: what it spends, what it wastes, what it should target, and what changed.</p>
<p class=note>All data on this page is simulated, with known problems planted on purpose so the rules can be scored. The pipeline reads the FinOps FOCUS billing format, so a real export can replace it.</p>
<h2>Headline</h2><div class=tiles>{tile_html}</div>
<h2>Now and target</h2>{_html_table(kpis, {"Metric": "Metric", "Now": "Now", "Target": "Target"})}
<h2>Cost to target</h2><img src="waterfall.png" alt="Waterfall from current monthly cost to target">
{_html_table(summary, {"label": "Opportunity", "resources": "Resources", "monthly_savings": "Monthly savings", "confidence": "Confidence", "expected_savings": "Expected savings"}, {"resources": str, "monthly_savings": k, "confidence": lambda v: f"{v:.0%}", "expected_savings": k})}
<h2>Platforms</h2>{_html_table(platforms, {"ServiceCategory": "Platform", "cost": "Monthly cost", "waste": "Identified savings", "efficiency": "Efficiency", "rule_coverage": "Rule coverage"}, {"cost": k, "waste": k, "efficiency": pct, "rule_coverage": pct})}
<div class=scroll><table><thead><tr><th>Platform</th><th>Unit metric</th><th class=n>Value</th></tr></thead><tbody>{unit_rows}</tbody></table></div>
<h2>Teams</h2><img src="teams.png" alt="Cost and identified waste by team">
{_html_table(teams, {"team": "Team", "cost": "Monthly cost", "waste": "Identified savings", "efficiency": "Efficiency", "share_of_cost": "Share of cost"}, {"cost": k, "waste": k, "efficiency": pct, "share_of_cost": pct})}
<h2>Trend</h2><img src="trend.png" alt="Monthly cost by platform and unit cost">
<h2>Anomalies</h2><img src="anomalies.png" alt="Daily cost anomalies">
{_html_table(flags, {"date": "Date", "team": "Team", "ServiceCategory": "Platform", "measure": "Measured as", "cost": "Cost", "expected": "Expected", "excess": "Excess"}, {"cost": k, "expected": k, "excess": k})}
<h2>AI tooling</h2>
{_html_table(ai, {"team": "Team", "spend": "Monthly spend", "weekday_users": "Weekday users", "acceptance_rate": "Acceptance rate", "cost_per_accepted": "Cost per accepted output"}, {"spend": k, "weekday_users": lambda v: f"{v:.0f}", "acceptance_rate": pct, "cost_per_accepted": lambda v: f"${v:.3f}"})}
<h2>Detection scorecard</h2>
{_html_table(score, {"type": "Problem", "planted": "Planted", "flagged": "Flagged", "correct": "Correct", "precision": "Precision", "recall": "Recall"}, {"planted": str, "flagged": str, "correct": str, "precision": pct, "recall": pct})}
<h2>Data checks</h2><ul>{checks}</ul>
<footer>Simulated data. Built by Sai Soumya Aloor. <a href="index.html">Try the interactive dashboard with your own bill</a>.</footer>
</main></body></html>
"""
    (DOCS / "report.html").write_text(page)
