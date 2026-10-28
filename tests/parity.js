// Runs the browser engine on a data folder and prints the key results as JSON.
// Used by tests/test_parity.py to check that the dashboard and the Python
// pipeline agree.
const fs = require("fs");
const path = require("path");
const zlib = require("zlib");
const engine = require("../docs/engine.js");

const dir = process.argv[2];
const read = name => {
  const p = path.join(dir, name);
  if (!fs.existsSync(p)) return undefined;
  const buf = fs.readFileSync(p);
  return (name.endsWith(".gz") ? zlib.gunzipSync(buf) : buf).toString("utf8");
};
const r = engine.analyze({
  bill: read("billing_focus.csv.gz"), telemetry: read("telemetry.csv.gz"), inventory: read("inventory.csv"),
  drivers: read("drivers.csv"), invoices: read("invoices.csv"),
});
console.log(JSON.stringify({
  checks: r.checks.map(c => ({ check: c.check, passed: c.passed })),
  company: r.company,
  summary: r.summary.map(s => ({ type: s.type, resources: s.resources, monthly_savings: s.monthly_savings })),
  teams: r.teams.map(t => ({ team: t.name, cost: t.cost, waste: t.waste })),
  platforms: r.platforms.map(p => ({ platform: p.name, cost: p.cost, rule_coverage: p.rule_coverage })),
  opportunities: r.opportunities.map(o => o.ResourceId).sort(),
  anomalies: r.anomalies.map(a => `${a.date}|${a.team}|${a.ServiceCategory}`),
  rawAnomalies: r.rawAnomalies.map(a => `${a.date}|${a.team}|${a.ServiceCategory}`),
}));
