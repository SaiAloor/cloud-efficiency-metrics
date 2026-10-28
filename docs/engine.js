/*
 * Cloud Efficiency Metrics: the calculation engine for the browser dashboard.
 *
 * This is a JavaScript port of the Python pipeline in efficiency/. It runs in
 * the browser, so a bill dropped into the page never leaves the visitor's
 * computer. tests/test_parity.py runs both versions on the sample data and
 * checks that they agree to the cent.
 *
 * No dependencies. Works in the browser (window.CostEngine) and in Node
 * (module.exports) so the parity test can call it.
 */
(function (root) {
  "use strict";

  const REQUIRED = [
    "BillingPeriodStart", "ChargePeriodStart", "ChargeCategory", "PricingCategory", "ServiceCategory",
    "ServiceName", "ResourceId", "ResourceType", "ConsumedQuantity", "ConsumedUnit",
    "ListCost", "EffectiveCost", "BilledCost", "Tags",
  ];
  const WINDOW_DAYS = 30;
  const IDLE_CPU = 0.05, IDLE_CPU_PEAK = 0.10, TARGET_PEAK = 0.65, OVERSIZED_PEAK = 0.40;
  const SIZES = [2, 4, 8, 16, 32, 64];
  const GPU_TARGET = 0.70, GPU_LOW = 0.30, STALE_DAYS = 90;
  const STD_RATE = 0.023, ARCHIVE_RATE = 0.004, COMMIT_DISCOUNT = 0.35;
  const CONFIDENCE = {
    orphaned_volume: 0.9, idle_compute: 0.9, stale_storage: 0.8, commitment_gap: 0.8,
    oversized_compute: 0.6, underused_gpu: 0.6,
  };
  const LABELS = {
    idle_compute: "Shut down idle machines", orphaned_volume: "Delete unattached disks",
    stale_storage: "Move unread data to archive", oversized_compute: "Right-size oversized machines",
    underused_gpu: "Consolidate underused GPUs", commitment_gap: "Cover steady usage with commitments",
  };
  const NEEDS = {
    orphaned_volume: "inventory", idle_compute: "telemetry", stale_storage: "inventory",
    oversized_compute: "telemetry", underused_gpu: "telemetry", commitment_gap: "bill",
  };
  const BASELINE_DAYS = 28, Z_LIMIT = 4.0, MIN_DOLLARS = 1000;
  const DRIVER_LINKED = new Set(["Networking"]);
  const DAY = 86400000;

  // ---------- CSV ----------
  function parseCSV(text) {
    const rows = [];
    let row = [], field = "", i = 0, q = false;
    const n = text.length;
    if (text.charCodeAt(0) === 0xfeff) i = 1;
    for (; i < n; i++) {
      const c = text[i];
      if (q) {
        if (c === '"') {
          if (text[i + 1] === '"') { field += '"'; i++; } else q = false;
        } else field += c;
      } else if (c === '"') q = true;
      else if (c === ",") { row.push(field); field = ""; }
      else if (c === "\n" || c === "\r") {
        if (c === "\r" && text[i + 1] === "\n") i++;
        row.push(field); field = "";
        if (row.length > 1 || row[0] !== "") rows.push(row);
        row = [];
      } else field += c;
    }
    if (field !== "" || row.length) { row.push(field); rows.push(row); }
    const head = rows.shift() || [];
    return { columns: head, rows };
  }

  function toObjects(parsed) {
    const { columns, rows } = parsed;
    return rows.map(r => { const o = {}; columns.forEach((c, j) => { o[c] = r[j] === undefined ? "" : r[j]; }); return o; });
  }

  // ---------- small helpers ----------
  const num = v => (v === "" || v === undefined || v === null ? NaN : Number(v));
  const day = s => String(s).slice(0, 10);
  const toDate = s => Date.parse(day(s) + "T00:00:00Z");
  const iso = t => new Date(t).toISOString().slice(0, 10);
  const sum = a => a.reduce((x, y) => x + y, 0);
  const mean = a => (a.length ? sum(a) / a.length : NaN);
  function median(a) {
    if (!a.length) return NaN;
    const s = [...a].sort((x, y) => x - y), m = s.length >> 1;
    return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
  }
  function quantile(a, q) {           // linear interpolation, same as pandas
    if (!a.length) return NaN;
    const s = [...a].sort((x, y) => x - y), pos = (s.length - 1) * q, lo = Math.floor(pos);
    return lo + 1 < s.length ? s[lo] + (s[lo + 1] - s[lo]) * (pos - lo) : s[lo];
  }
  function groupBy(rows, keyFn) {
    const m = new Map();
    for (const r of rows) { const k = keyFn(r); if (!m.has(k)) m.set(k, []); m.get(k).push(r); }
    return m;
  }
  function parseTags(s) {
    if (!s) return {};
    try { const t = JSON.parse(s); return t && typeof t === "object" ? t : {}; } catch (e) { return {}; }
  }

  // ---------- load and check ----------
  function prepareBill(text) {
    const parsed = parseCSV(text);
    const checks = [];
    const missing = REQUIRED.filter(c => !parsed.columns.includes(c));
    checks.push({ check: "FOCUS columns present", passed: !missing.length, level: "error",
      detail: missing.length ? "missing: " + missing.join(", ") : "all required columns found" });
    if (missing.length) return { bill: [], checks };

    const seen = new Set(), keep = [];
    let dupN = 0, dupDollars = 0;
    const dupDays = new Set();
    const ci = Object.fromEntries(parsed.columns.map((c, j) => [c, j]));
    for (const r of parsed.rows) {
      const key = r.join("\u0001");
      if (seen.has(key)) {
        dupN++; dupDollars += num(r[ci.BilledCost]) || 0; dupDays.add(day(r[ci.ChargePeriodStart]));
        continue;
      }
      seen.add(key); keep.push(r);
    }
    checks.push({ check: "Duplicate line items", passed: true, level: "warning",
      detail: dupN ? `removed ${dupN.toLocaleString("en-US")} duplicated line items worth $${dupDollars.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })} (days: ${[...dupDays].sort().join(", ")})` : "no duplicate line items" });

    const bill = keep.map(r => {
      const tags = parseTags(r[ci.Tags]);
      return {
        period: day(r[ci.BillingPeriodStart]), date: day(r[ci.ChargePeriodStart]),
        charge: r[ci.ChargeCategory], pricing: r[ci.PricingCategory], category: r[ci.ServiceCategory],
        service: r[ci.ServiceName], id: r[ci.ResourceId], type: r[ci.ResourceType], sku: ci.SkuId !== undefined ? r[ci.SkuId] : "",
        qty: num(r[ci.ConsumedQuantity]) || 0, unit: r[ci.ConsumedUnit],
        list: num(r[ci.ListCost]) || 0, eff: num(r[ci.EffectiveCost]) || 0, billed: num(r[ci.BilledCost]) || 0,
        team: tags.team || null, env: tags.env || null, app: tags.app || null,
      };
    });
    return { bill, checks };
  }

  function checkBill(bill, invoices) {
    const out = [];
    const money = v => "$" + v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    const signed = v => (v >= 0 ? "+" : "-") + money(Math.abs(v));
    if (invoices && invoices.length) {
      const billed = new Map();
      for (const r of bill) billed.set(r.period, (billed.get(r.period) || 0) + r.billed);
      for (const inv of invoices) {
        const p = day(inv.BillingPeriodStart), got = billed.get(p) || 0, gap = got - num(inv.InvoiceTotal);
        out.push({ check: `Invoice match ${p.slice(0, 7)}`, passed: Math.abs(gap) <= 1, level: "error",
          detail: `billed ${money(got)} vs invoice ${money(num(inv.InvoiceTotal))} (gap ${signed(gap)})` });
      }
    } else {
      out.push({ check: "Invoice match", passed: true, level: "warning", detail: "no invoice file provided, skipped" });
    }
    const purchase = sum(bill.filter(r => r.charge === "Purchase").map(r => r.billed));
    const amort = sum(bill.filter(r => r.pricing === "Commitment-Based" && r.charge === "Usage").map(r => r.eff));
    out.push({ check: "Commitments fully amortized", passed: Math.abs(purchase - amort) <= 1, level: "error",
      detail: `purchases ${money(purchase)} vs amortized usage ${money(amort)} (gap ${signed(purchase - amort)})` });
    const neg = bill.filter(r => r.eff < 0 && r.charge !== "Credit").length;
    out.push({ check: "Negative cost only on credits", passed: !neg, level: "error", detail: neg ? `${neg} negative usage lines` : "ok" });
    const bad = bill.filter(r => r.date.slice(0, 7) !== r.period.slice(0, 7)).length;
    out.push({ check: "Charges inside their billing month", passed: !bad, level: "error", detail: bad ? `${bad} lines dated outside their billing month` : "ok" });
    const usage = bill.filter(r => r.charge === "Usage");
    const tot = sum(usage.map(r => r.eff)), tagged = sum(usage.filter(r => r.team).map(r => r.eff));
    const share = tot ? tagged / tot : 0;
    out.push({ check: "Team tag coverage", passed: share >= 0.95, level: "warning",
      detail: `${(share * 100).toFixed(1)}% of usage cost has a team tag (target 95%)` });
    return out;
  }

  // ---------- analysis ----------
  function windowOf(bill) {
    const end = Math.max(...bill.map(r => toDate(r.date)));
    return { start: end - (WINDOW_DAYS - 1) * DAY, end };
  }

  function resources(bill, win) {
    const usage = bill.filter(r => r.charge === "Usage" && toDate(r.date) >= win.start && toDate(r.date) <= win.end);
    const res = new Map();
    for (const r of usage) {
      let o = res.get(r.id);
      if (!o) { o = { id: r.id, category: r.category, service: r.service, pricing: r.pricing, team: r.team || "untagged", env: r.env, cost: 0, dates: new Set() }; res.set(r.id, o); }
      o.cost += r.eff; o.dates.add(r.date);
    }
    for (const o of res.values()) o.days = o.dates.size;
    return { usage, res };
  }

  function attachTelemetry(res, telemetry, win) {
    if (!telemetry) return false;
    const inWin = telemetry.filter(t => { const d = toDate(t.date); return d >= win.start && d <= win.end; });
    const last14 = win.end - 14 * DAY;
    for (const [id, rows] of groupBy(inWin, t => t.ResourceId)) {
      const o = res.get(id); if (!o) continue;
      const cpuAvg = rows.map(t => num(t.cpu_avg)).filter(Number.isFinite);
      const cpuP95 = rows.map(t => num(t.cpu_p95)).filter(Number.isFinite);
      const gpu = rows.map(t => num(t.gpu_avg)).filter(Number.isFinite);
      const recent = rows.filter(t => toDate(t.date) > last14);
      o.vcpu = Math.max(...rows.map(t => num(t.vcpu)).filter(Number.isFinite), -Infinity);
      o.gpus = Math.max(...rows.map(t => num(t.gpus)).filter(Number.isFinite), -Infinity);
      o.cpu_avg = mean(cpuAvg); o.cpu_peak = quantile(cpuP95, 0.95); o.gpu_avg = mean(gpu);
      o.avg14 = mean(recent.map(t => num(t.cpu_avg)).filter(Number.isFinite));
      o.peak14 = Math.max(...recent.map(t => num(t.cpu_p95)).filter(Number.isFinite), -Infinity);
      o.inTelemetry = true;
    }
    return true;
  }

  function attachInventory(res, inventory) {
    if (!inventory) return false;
    for (const r of inventory) {
      const o = res.get(r.ResourceId); if (!o) continue;
      o.tier = r.storage_tier; o.last_read = r.last_read; o.attached = r.attached; o.inInventory = true;
    }
    return true;
  }

  function findOpportunities(res, win, have) {
    const out = [], taken = new Set();
    const list = [...res.values()].sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
    const vm = o => Number.isFinite(o.vcpu) && o.vcpu > 0;
    const add = (kind, pred, action, savings) => {
      if (have[NEEDS[kind]] === false) return;
      for (const o of list) {
        if (taken.has(o.id) || !pred(o)) continue;
        const s = savings(o);
        if (s > 0) {
          out.push({ ResourceId: o.id, team: o.team, ServiceCategory: o.category, type: kind, label: LABELS[kind],
            action: action(o), monthly_cost: o.cost, monthly_savings: s, confidence: CONFIDENCE[kind],
            expected_savings: s * CONFIDENCE[kind] });
          taken.add(o.id);
        }
      }
    };
    const newSize = o => { const need = o.vcpu * o.cpu_peak / TARGET_PEAK; const s = SIZES.find(x => x >= need); return s === undefined ? o.vcpu : s; };
    const gpuKeep = o => Math.max(1, Math.ceil(o.gpus * o.gpu_avg / GPU_TARGET));
    const staleCut = iso(win.end - STALE_DAYS * DAY);

    add("orphaned_volume", o => o.attached === "False", () => "snapshot, then delete", o => o.cost);
    add("idle_compute", o => vm(o) && o.avg14 < IDLE_CPU && o.peak14 < IDLE_CPU_PEAK, o => `shut down (${o.env || "no env tag"})`, o => o.cost);
    add("stale_storage", o => o.tier === "standard" && o.last_read && o.last_read < staleCut, o => `archive (last read ${o.last_read})`, o => o.cost * (1 - ARCHIVE_RATE / STD_RATE));
    add("oversized_compute", o => vm(o) && o.cpu_peak < OVERSIZED_PEAK && o.vcpu >= 4, o => `${o.vcpu} -> ${newSize(o)} vCPU`, o => o.cost * (1 - newSize(o) / o.vcpu));
    add("underused_gpu", o => Number.isFinite(o.gpus) && o.gpus > 0 && o.gpu_avg < GPU_LOW, o => `${o.gpus} -> ${gpuKeep(o)} GPUs`, o => o.cost * (1 - gpuKeep(o) / o.gpus));
    add("commitment_gap", o => o.category === "Compute" && o.pricing === "On-Demand" && (o.env === "prod" || o.env === null) && o.days === WINDOW_DAYS,
      () => "move to 1-year commitment", o => o.cost * COMMIT_DISCOUNT);
    return out.sort((a, b) => b.monthly_savings - a.monthly_savings);
  }

  function summarize(opps) {
    const g = groupBy(opps, o => o.type);
    return [...g].map(([type, rows]) => ({
      type, label: LABELS[type], resources: rows.length, monthly_savings: sum(rows.map(r => r.monthly_savings)),
      expected_savings: sum(rows.map(r => r.expected_savings)), confidence: CONFIDENCE[type],
    })).sort((a, b) => b.expected_savings - a.expected_savings);
  }

  function covered(o, have) {
    return (have.inventory && o.inInventory) || (have.telemetry && o.inTelemetry) || o.category === "Compute";
  }

  function tree(usage, res, opps, drivers, win, have) {
    const level = key => {
      const cost = new Map(), waste = new Map(), exp = new Map(), cov = new Map();
      for (const r of usage) { const k = key === "team" ? (r.team || "untagged") : r.category; cost.set(k, (cost.get(k) || 0) + r.eff); }
      for (const o of opps) { const k = key === "team" ? o.team : o.ServiceCategory; waste.set(k, (waste.get(k) || 0) + o.monthly_savings); exp.set(k, (exp.get(k) || 0) + o.expected_savings); }
      for (const o of res.values()) if (covered(o, have)) { const k = key === "team" ? o.team : o.category; cov.set(k, (cov.get(k) || 0) + o.cost); }
      const total = sum([...cost.values()]);
      return [...cost].map(([name, c]) => ({ name, cost: c, waste: waste.get(name) || 0, expected_savings: exp.get(name) || 0,
        efficiency: 1 - (waste.get(name) || 0) / c, share_of_cost: c / total, rule_coverage: (cov.get(name) || 0) / c }))
        .sort((a, b) => b.cost - a.cost);
    };
    const teams = level("team"), platforms = level("category");
    const total = sum(usage.map(r => r.eff));
    const waste = sum(opps.map(o => o.monthly_savings)), expected = sum(opps.map(o => o.expected_savings));
    let hours = NaN;
    if (drivers) hours = sum(drivers.filter(d => { const t = toDate(d.date); return t >= win.start && t <= win.end; }).map(d => num(d.streaming_hours)));
    const tagged = sum(usage.filter(r => r.team).map(r => r.eff));
    const cov = sum([...res.values()].filter(o => covered(o, have)).map(o => o.cost));
    const company = {
      window: `${iso(win.start)} to ${iso(win.end)}`, cost: total, waste, expected_savings: expected,
      efficiency: 1 - waste / total, target_efficiency: 1 - (waste - expected) / (total - expected),
      target_cost: total - expected, cost_per_1k_hours: total / hours * 1000,
      target_cost_per_1k_hours: (total - expected) / hours * 1000, tag_coverage: tagged / total, rule_coverage: cov / total,
    };
    return { company, teams, platforms };
  }

  function monthly(bill, drivers) {
    const usage = bill.filter(r => r.charge === "Usage");
    const months = new Map(), cats = new Set();
    for (const r of usage) {
      const m = r.period.slice(0, 7); cats.add(r.category);
      if (!months.has(m)) months.set(m, { month: m, total: 0, byCat: {} });
      const o = months.get(m); o.total += r.eff; o.byCat[r.category] = (o.byCat[r.category] || 0) + r.eff;
    }
    if (drivers) for (const d of drivers) { const o = months.get(day(d.date).slice(0, 7)); if (o) o.hours = (o.hours || 0) + num(d.streaming_hours); }
    const rows = [...months.values()].sort((a, b) => (a.month < b.month ? -1 : 1));
    for (const o of rows) o.cost_per_1k_hours = o.hours ? o.total / o.hours * 1000 : NaN;
    return { rows, categories: [...cats] };
  }

  function robustZ(values) {
    return values.map((v, i) => {
      if (i < BASELINE_DAYS) return { baseline: NaN, z: NaN };
      const w = values.slice(i - BASELINE_DAYS, i), med = median(w);
      const mad = median(w.map(x => Math.abs(x - med)));
      const scale = Math.max(1.4826 * mad, 0.01 * Math.abs(med));
      return { baseline: med, z: (v - med) / scale };
    });
  }

  function dailySeries(bill, drivers) {
    const hours = new Map((drivers || []).map(d => [day(d.date), num(d.streaming_hours)]));
    const usage = bill.filter(r => r.charge === "Usage");
    const g = new Map();
    for (const r of usage) {
      const k = (r.team || "untagged") + "\u0001" + r.category;
      if (!g.has(k)) g.set(k, new Map());
      const m = g.get(k); m.set(r.date, (m.get(r.date) || 0) + r.eff);
    }
    return [...g].map(([k, m]) => {
      const [team, category] = k.split("\u0001");
      const dates = [...m.keys()].sort();
      return { team, category, dates, cost: dates.map(d => m.get(d)), hours: dates.map(d => hours.get(d)) };
    });
  }

  function anomalies(series, normalize, haveDrivers) {
    const flags = [];
    for (const s of series) {
      const perHour = normalize && haveDrivers && DRIVER_LINKED.has(s.category);
      const vals = perHour ? s.cost.map((c, i) => c / s.hours[i] * 1000) : s.cost;
      const z = robustZ(vals);
      z.forEach((r, i) => {
        if (!Number.isFinite(r.z)) return;
        const expected = perHour ? r.baseline * s.hours[i] / 1000 : r.baseline;
        const excess = s.cost[i] - expected;
        if (Math.abs(r.z) >= Z_LIMIT && Math.abs(excess) >= MIN_DOLLARS)
          flags.push({ date: s.dates[i], team: s.team, ServiceCategory: s.category,
            measure: perHour ? "cost per 1,000 streaming hours" : "daily cost", cost: s.cost[i], expected, excess, z: r.z });
      });
    }
    return flags.sort((a, b) => (a.date < b.date ? -1 : a.date > b.date ? 1 : a.team < b.team ? -1 : 1));
  }

  // ---------- entry point ----------
  /**
   * files: { bill: string (CSV text), telemetry?, inventory?, drivers?, invoices? (CSV text) }
   */
  function analyze(files) {
    const { bill, checks } = prepareBill(files.bill);
    if (!bill.length) return { checks, failed: true };
    const objs = k => (files[k] ? toObjects(parseCSV(files[k])) : null);
    const telemetry = objs("telemetry"), inventory = objs("inventory"), drivers = objs("drivers"), invoices = objs("invoices");
    checks.push(...checkBill(bill, invoices));

    const win = windowOf(bill);
    const { usage, res } = resources(bill, win);
    const have = { bill: true, telemetry: attachTelemetry(res, telemetry, win), inventory: attachInventory(res, inventory), drivers: !!drivers };
    const opps = findOpportunities(res, win, have);
    const t = tree(usage, res, opps, drivers, win, have);
    const series = dailySeries(bill, drivers);
    return {
      checks, failed: checks.some(c => !c.passed && c.level === "error"), have,
      skippedRules: Object.keys(NEEDS).filter(k => have[NEEDS[k]] === false).map(k => LABELS[k]),
      window: win, ...t, opportunities: opps, summary: summarize(opps), monthly: monthly(bill, drivers),
      anomalies: anomalies(series, true, !!drivers), rawAnomalies: anomalies(series, false, !!drivers), series,
    };
  }

  const api = { analyze, parseCSV, robustZ, LABELS };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.CostEngine = api;
})(typeof window !== "undefined" ? window : globalThis);
