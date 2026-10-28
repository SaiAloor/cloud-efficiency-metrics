"""The browser dashboard (docs/engine.js) must agree with the Python pipeline."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from efficiency import anomalies, load, metrics, opportunities
from efficiency.simulate import simulate

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")


@pytest.fixture(scope="module")
def both(tmp_path_factory):
    if not NODE:
        pytest.skip("Node.js is not installed")
    d = tmp_path_factory.mktemp("data")
    simulate(out=d)
    js = json.loads(subprocess.run([NODE, str(ROOT / "tests" / "parity.js"), str(d)],
                                   capture_output=True, text=True, check=True).stdout)
    ds = load.load(d)
    opps = opportunities.find(ds)
    return js, ds, opps, metrics.tree(ds, opps)


def test_same_checks(both):
    js, ds, _, _ = both
    py = {c.name: bool(c.passed) for c in ds.checks if c.name != "FOCUS columns present"}
    js_checks = {c["check"]: c["passed"] for c in js["checks"] if c["check"] != "FOCUS columns present"}
    assert js_checks == py


def test_same_company_numbers(both):
    js, _, _, t = both
    for key in ["cost", "waste", "expected_savings", "efficiency", "target_cost", "cost_per_1k_hours",
                "tag_coverage", "rule_coverage"]:
        assert js["company"][key] == pytest.approx(t["company"][key], rel=1e-9, abs=0.01), key


def test_same_opportunities(both):
    js, _, opps, _ = both
    assert js["opportunities"] == sorted(opps["ResourceId"])
    py = opportunities.summarize(opps).set_index("type")
    for s in js["summary"]:
        assert s["resources"] == py.loc[s["type"], "resources"]
        assert s["monthly_savings"] == pytest.approx(py.loc[s["type"], "monthly_savings"], abs=0.01)


def test_same_teams_and_platforms(both):
    js, _, _, t = both
    teams = t["teams"].set_index("team")
    for row in js["teams"]:
        assert row["cost"] == pytest.approx(teams.loc[row["team"], "cost"], abs=0.01)
        assert row["waste"] == pytest.approx(teams.loc[row["team"], "waste"], abs=0.01)
    plats = t["platforms"].set_index("ServiceCategory")
    for row in js["platforms"]:
        assert row["rule_coverage"] == pytest.approx(plats.loc[row["platform"], "rule_coverage"], abs=1e-9)


def test_same_anomalies(both):
    js, ds, _, _ = both
    key = lambda df: [f"{r.date}|{r.team}|{r.ServiceCategory}" for r in df.itertuples()]  # noqa: E731
    assert js["anomalies"] == key(anomalies.detect(ds))
    assert js["rawAnomalies"] == key(anomalies.detect(ds, normalize=False))
