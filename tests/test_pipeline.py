import pandas as pd
import pytest

from efficiency import anomalies, load, metrics, opportunities
from efficiency.simulate import simulate


@pytest.fixture(scope="module")
def ds(tmp_path_factory):
    d = tmp_path_factory.mktemp("data")
    simulate(out=d)
    return load.load(d), pd.read_csv(d / "planted_issues.csv")


def test_all_data_checks_pass(ds):
    data, _ = ds
    assert data.failed == []


def test_duplicate_export_is_removed(ds):
    data, _ = ds
    dup = next(c for c in data.checks if c.name == "Duplicate line items")
    assert "removed 300" in dup.detail
    assert not data.bill.duplicated(subset=[c for c in data.bill.columns if c not in ("date", "month", "team", "env", "app")]).any()


def test_invoice_mismatch_fails(ds):
    data, _ = ds
    bad = data.invoices.copy()
    bad.loc[0, "InvoiceTotal"] += 500
    checks = load.check_invoices(data.bill, bad)
    assert not checks[0].passed and all(c.passed for c in checks[1:])


def test_metric_tree_adds_up(ds):
    data, _ = ds
    opps = opportunities.find(data)
    t = metrics.tree(data, opps)
    c = t["company"]
    assert t["teams"]["cost"].sum() == pytest.approx(c["cost"])
    assert t["platforms"]["cost"].sum() == pytest.approx(c["cost"])
    assert t["teams"]["waste"].sum() == pytest.approx(c["waste"])
    assert t["platforms"]["waste"].sum() == pytest.approx(c["waste"])
    m = metrics.team_by_platform(data)
    assert m.loc["Total", "Total"] == pytest.approx(c["cost"], abs=0.05)


def test_company_efficiency_is_ratio_of_sums(ds):
    data, _ = ds
    opps = opportunities.find(data)
    t = metrics.tree(data, opps)
    teams = t["teams"]
    assert t["company"]["efficiency"] == pytest.approx(1 - teams["waste"].sum() / teams["cost"].sum())


def test_no_resource_counted_twice(ds):
    data, _ = ds
    opps = opportunities.find(data)
    assert opps["ResourceId"].is_unique
    assert (opps["monthly_savings"] <= opps["monthly_cost"] + 1e-9).all()


def test_rules_find_most_planted_problems(ds):
    data, planted = ds
    score = opportunities.score_against_planted(opportunities.find(data), planted).set_index("type")
    assert (score["recall"] >= 0.85).all()
    assert (score["precision"] >= 0.85).all()


def test_runaway_job_is_flagged(ds):
    data, _ = ds
    flags = anomalies.detect(data)
    hit = flags[(flags["team"] == "data") & (flags["ServiceCategory"] == "Compute")]
    assert set(hit["date"]) == {"2026-08-14", "2026-08-15", "2026-08-16", "2026-08-17"}


def test_launch_weekend_is_not_a_false_alarm(ds):
    data, _ = ds
    raw = anomalies.detect(data, normalize=False)
    normalized = anomalies.detect(data)
    assert (raw["ServiceCategory"] == "Networking").any()
    assert not (normalized["ServiceCategory"] == "Networking").any()


def test_robust_z_ignores_one_bad_day():
    s = pd.Series([100.0] * 40)
    s.iloc[35] = 500
    z = anomalies.robust_z(s, baseline=28)
    assert z["z"].iloc[35] > 4
    assert abs(z["z"].iloc[36]) < 1  # the spike did not move the baseline
