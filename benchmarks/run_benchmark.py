"""End-to-end AMLworld benchmark: HELIX export + cached window -> results.

    backend\\.venv\\Scripts\\python.exe benchmarks\\run_benchmark.py

Inputs: benchmarks/.cache/window.csv (prepare_data.py), benchmarks/results/helix_alerts.csv
(export_helix.py), benchmarks/config.json. Outputs in benchmarks/results/: results.csv,
results.md, summary.json, value_per_hour_N50.png. Deterministic for a fixed config.
"""

from __future__ import annotations

import argparse
import json
import time

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from sklearn.metrics import average_precision_score, roc_auc_score  # noqa: E402

from baselines import add_features, iforest_scores, rule_scores, xgb_scores  # noqa: E402
from common import CACHE, RESULTS, load_config, read_window  # noqa: E402
from scoring import Costs, evaluate, group_alerts, helix_alerts, top_n_per_hour  # noqa: E402

HELIX_A = "HELIX (Active_Queue, own budget 20/h)"
HELIX_B = "HELIX + Backlog (equal capacity)"
RULE, IF_, XGB = "Rule (P99 amount / 1h burst)", "Isolation Forest", "XGBoost (supervised)"
ORDER = [HELIX_A, HELIX_B, RULE, IF_, XGB]
KEY = ["alerts", "tp", "fp", "alert_precision", "precision_at_n", "tx_recall", "value_captured_usd",
       "value_captured_frac", "investigator_hours", "value_per_inv_hour_usd", "cost_per_true_catch_usd",
       "wasted_hours", "value_capped_usd", "value_capped_frac", "value_capped_per_inv_hour_usd"]


def _ts(s: str) -> pd.Timestamp:
    return pd.Timestamp(pd.to_datetime(s, format="%Y/%m/%d %H:%M"), tz="UTC")


def _auc(y: np.ndarray, s: np.ndarray) -> tuple[float | None, float | None]:
    if y.min() == y.max():
        return None, None
    return float(roc_auc_score(y, s)), float(average_precision_score(y, s))


def triage_summary(test: pd.DataFrame, cfg: dict, stem: str = "helix_alerts_triage") -> dict | None:
    """Auto_Triage export (export_helix.py --triage): LOW Cases auto-cleared, hours saved and
    the laundering inside them. None when that export was not run."""
    path = RESULTS / f"{stem}.csv"
    if not path.exists():
        return None
    c = pd.read_csv(path, dtype=str, keep_default_na=False)
    cl = c[c["closure_reason"] == "AUTO_CLEARED"]
    lab = test.loc[test["y"] == 1]
    lab_usd = dict(zip(lab["tx_id"], lab["usd"]))
    inside = {t for ids in cl["tx_ids"] for t in ids.split(";") if t in lab_usd}
    was_active = cl[cl["active_queue"] == "yes"]
    mins = cfg["minutes_per_case"]
    run = json.loads((RESULTS / f"{stem}.run.json").read_text())
    return {
        "config": run["config"],
        "cases_total": int(len(c)),
        "auto_cleared": int(len(cl)),
        "auto_cleared_low_tier": int(cl["tier"].str.endswith("LOW").sum()),
        "auto_cleared_ever_in_active_queue": int(len(was_active)),
        "hours_saved_if_all_would_be_worked": round(len(cl) * mins / 60, 1),
        "hours_saved_active_queue_only": round(len(was_active) * mins / 60, 1),
        "laundering_tx_inside": len(inside),
        "laundering_usd_inside": round(float(sum(lab_usd[t] for t in inside)), 2),
        "cases_with_laundering_inside": int(sum(any(t in lab_usd for t in ids.split(";")) for ids in cl["tx_ids"])),
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--helix", default="helix_alerts", help="stem of the HELIX export in results/")
    a = p.parse_args()
    t0 = time.monotonic()
    cfg = load_config()
    w = cfg["windows"]
    fit_start, test_start, test_end = _ts(w["fit_start"]), _ts(w["test_start"]), _ts(w["test_end"])
    hours = int((test_end - test_start) / pd.Timedelta(hours=1))

    df = add_features(read_window(cfg=cfg))
    fit = df[(df["ts"] >= fit_start) & (df["ts"] < test_start)].reset_index(drop=True)
    test = df[(df["ts"] >= test_start) & (df["ts"] < test_end)].reset_index(drop=True)
    y = test["y"].to_numpy()
    # Rows HELIX's TransactionEvent schema rejects (amount after rounding to cents outside
    # [0.01, 999999999.99]); HELIX never sees them, the baselines do.
    cents = test["amount"].round(2)
    unseen = (cents < 0.01) | (cents > 999_999_999.99)
    lab_usd_total = float(test.loc[test["y"] == 1, "usd"].sum())
    helix_unseen = {"rows": int(unseen.sum()), "laundering": int((unseen & (test["y"] == 1)).sum()),
                    "laundering_usd": round(float(test.loc[unseen & (test["y"] == 1), "usd"].sum()), 2)}
    helix_unseen["laundering_value_share"] = helix_unseen["laundering_usd"] / lab_usd_total if lab_usd_total else 0.0
    print(f"rows HELIX cannot ingest: {helix_unseen}")
    print(f"fit rows {len(fit):,} ({fit['y'].sum()} laundering), test rows {len(test):,} ({y.sum()} laundering)")

    # ---- per-system test-transaction scores and alert lists
    runs: dict[str, dict[int | None, pd.DataFrame]] = {}
    tx_scores: dict[str, dict[int | None, np.ndarray]] = {}
    params: dict = {}
    rs, params["rule"] = rule_scores(fit, test)
    runs[RULE] = {None: group_alerts(rs, test)}
    tx_scores[RULE] = {None: rs["score"].to_numpy()}
    runs[IF_], runs[XGB], tx_scores[IF_], tx_scores[XGB] = {}, {}, {}, {}
    for seed in cfg["seeds"]:
        s = iforest_scores(fit, test, seed)
        runs[IF_][seed], tx_scores[IF_][seed] = group_alerts(s, test), s["score"].to_numpy()
        s, params["xgboost"] = xgb_scores(fit, test, seed)
        runs[XGB][seed], tx_scores[XGB][seed] = group_alerts(s, test), s["score"].to_numpy()
    print(f"baselines scored in {time.monotonic() - t0:.0f} s")

    cases = pd.read_csv(RESULTS / f"{a.helix}.csv", dtype={"merged_into": str, "tx_ids": str, "placements": str},
                        keep_default_na=False)
    runs[HELIX_A] = {None: helix_alerts(cases, test, test_start, test_end, include_backlog=False)}
    runs[HELIX_B] = {None: helix_alerts(cases, test, test_start, test_end, include_backlog=True)}
    # HELIX transaction score for ROC/PR: max Risk_Score of a surviving Case holding it, else 0.
    risk: dict[str, float] = {}
    for c in cases.itertuples(index=False):
        if c.merged_into:
            continue
        for t in str(c.tx_ids).split(";"):
            risk[t] = max(risk.get(t, 0.0), float(c.risk))
    hs = test["tx_id"].map(risk).fillna(0.0).to_numpy()
    tx_scores[HELIX_A] = {None: hs}
    tx_scores[HELIX_B] = {None: hs}
    helix_run = json.loads((RESULTS / f"{a.helix}.run.json").read_text())
    triage = triage_summary(test, cfg)

    # ---- metrics
    rows = []
    for sysname in ORDER:
        for seed, alerts in runs[sysname].items():
            for n in cfg["capacities"]:
                sel = top_n_per_hour(alerts, n)
                for minutes in cfg["minutes_sensitivity"]:
                    costs = Costs(minutes, cfg["analyst_cost_per_hour"], cfg["cost_per_case"])
                    m = evaluate(sel, test, costs, hours, n, cfg["value_cap_usd"])
                    rows.append({"system": sysname, "seed": seed, "N": n, "minutes_per_case": minutes,
                                 "candidate_alerts": len(alerts), **m})
    res = pd.DataFrame(rows)
    RESULTS.mkdir(parents=True, exist_ok=True)
    res.to_csv(RESULTS / "results.csv", index=False)

    auc = {}
    for sysname in ORDER:
        vals = [_auc(y, s) for s in tx_scores[sysname].values()]
        auc[sysname] = {"roc_auc": [v[0] for v in vals], "pr_auc": [v[1] for v in vals]}

    base_min = cfg["minutes_per_case"]
    main_seed = cfg["seed"]
    head = res[(res["minutes_per_case"] == base_min) & (res["seed"].isna() | (res["seed"] == main_seed))]

    def seed_range(sysname: str, n: int, col: str) -> list | None:
        r = res[(res["system"] == sysname) & (res["N"] == n) & (res["minutes_per_case"] == base_min)]
        if r["seed"].isna().all():
            return None
        return [float(r[col].min()), float(r[col].max())]

    # ---- summary.json (read by the frontend)
    summary = {
        "dataset": "IBM AMLworld HI-Small (data/HI-Small_Trans.csv)",
        "windows": {
            "baseline_fit": [w["fit_start"], w["test_start"]],
            "helix_warmup": [w["helix_warmup_start"], w["test_start"]],
            "test": [w["test_start"], w["test_end"]],
            "end_exclusive": True, "timezone": "UTC (AMLworld timestamps are naive)",
        },
        "rows": {"fit": int(len(fit)), "fit_laundering": int(fit["y"].sum()), "test": int(len(test)),
                 "test_laundering": int(y.sum()),
                 "test_laundering_usd": round(float(test.loc[test["y"] == 1, "usd"].sum()), 2),
                 "helix_warmup": helix_run["warmup_rows"], "helix_test": helix_run["test_rows"],
                 "test_rows_helix_cannot_ingest": helix_unseen},
        "test_hours": hours,
        "assumptions": {
            "alert_unit": "one alert = one account investigation; baselines group flagged tx by (clock hour, source account), score = max; HELIX alert = one Case in the hour it was admitted (Backlog variant: created)",
            "true_positive": "alert holds >= 1 laundering transaction",
            "capacity": "each system takes its own top N alerts per clock hour",
            "minutes_per_case": base_min, "analyst_cost_per_hour": cfg["analyst_cost_per_hour"],
            "cost_per_case": cfg["cost_per_case"], "cost_per_alert": Costs(base_min, cfg["analyst_cost_per_hour"], cfg["cost_per_case"]).cost_per_alert,
            "currency": "Amount Paid x fixed approximate USD rate table (benchmarks/config.json)",
            "value_cap_usd": cfg["value_cap_usd"], "value_cap_note": cfg["value_cap_note"],
            "helix_config": helix_run["config"], "helix_alert_budget_per_hour": helix_run["alert_budget_per_period"],
            "baseline_params": params,
            "scope_note": "on AMLworld test data only; not a real-world savings estimate",
        },
        "seeds": cfg["seeds"], "headline_seed": main_seed,
        "systems": {},
    }
    for sysname in ORDER:
        per_n = {}
        for n in cfg["capacities"]:
            r = head[(head["system"] == sysname) & (head["N"] == n)].iloc[0]
            d = {k: (None if pd.isna(r[k]) else (float(r[k]) if isinstance(r[k], (float, np.floating)) else int(r[k])))
                 for k in KEY}
            rng = {k: seed_range(sysname, n, k) for k in ("tp", "value_captured_usd", "value_per_inv_hour_usd", "value_capped_per_inv_hour_usd",
                                                          "alert_precision", "tx_recall")}
            d["seed_range"] = rng if any(v is not None for v in rng.values()) else None
            per_n[str(n)] = d
        sens = {}
        for minutes in cfg["minutes_sensitivity"]:
            r = res[(res["system"] == sysname) & (res["N"] == cfg["headline_capacity"]) &
                    (res["minutes_per_case"] == minutes) & (res["seed"].isna() | (res["seed"] == main_seed))].iloc[0]
            sens[str(minutes)] = {"value_per_inv_hour_usd": float(r["value_per_inv_hour_usd"]),
                                  "cost_per_true_catch_usd": None if pd.isna(r["cost_per_true_catch_usd"]) else float(r["cost_per_true_catch_usd"]),
                                  "investigator_hours": float(r["investigator_hours"])}
        summary["systems"][sysname] = {
            "candidate_alerts": int(head[head["system"] == sysname]["candidate_alerts"].iloc[0]),
            "by_capacity": per_n,
            "roc_auc": auc[sysname]["roc_auc"], "pr_auc": auc[sysname]["pr_auc"],
            "minutes_sensitivity_N%d" % cfg["headline_capacity"]: sens,
        }
    summary["helix_run"] = helix_run
    summary["auto_triage"] = triage if triage is not None else "not run"
    (RESULTS / "summary.json").write_text(json.dumps(summary, indent=2))

    # ---- results.md
    md = [f"# AMLworld HI-Small benchmark results\n",
          f"Test window {w['test_start']} .. {w['test_end']} UTC ({hours} h, {len(test):,} tx, "
          f"{int(y.sum())} laundering, {summary['rows']['test_laundering_usd']:,.0f} USD laundering value). "
          f"Baselines fit on {w['fit_start']} .. {w['test_start']} ({len(fit):,} tx). HELIX warm-up "
          f"{w['helix_warmup_start']} .. {w['test_start']} ({helix_run['warmup_rows']:,} tx, Warm_Up_Mode), default config.",
          f"Costs: {base_min} min/case, {cfg['analyst_cost_per_hour']}/h analyst, {cfg['cost_per_case']}/case. "
          f"IF/XGBoost: seed {main_seed} shown, range over seeds {cfg['seeds']} in brackets. On AMLworld test data only.\n"]
    for n in cfg["capacities"]:
        md.append(f"## N = {n} alerts per hour (capacity {n * hours})\n")
        md.append("| System | Alerts | TP | FP | Alert precision | Precision@N | Tx recall | Value captured (USD) | "
                  "Share of laundering value | Inv. hours | Value / inv. hour (USD) | Cost per true catch (USD) | Wasted hours | "
                  f"Capped value share (cap {cfg['value_cap_usd']:,} USD/tx) | Capped value / inv. hour (USD) |")
        md.append("|" + "---|" * 15)
        for sysname in ORDER:
            d = summary["systems"][sysname]["by_capacity"][str(n)]
            rg = d["seed_range"]
            def f(k, fmt):
                v = d[k]
                s = "n/a" if v is None else format(v, fmt)
                if rg and rg.get(k):
                    lo, hi = rg[k]
                    s += f" [{format(lo, fmt)}-{format(hi, fmt)}]"
                return s
            md.append(f"| {sysname} | {d['alerts']} | {f('tp', '.0f')} | {d['fp']} | {f('alert_precision', '.3f')} | "
                      f"{d['precision_at_n']:.3f} | {f('tx_recall', '.3f')} | {f('value_captured_usd', ',.0f')} | "
                      f"{d['value_captured_frac']:.3f} | {d['investigator_hours']:.0f} | {f('value_per_inv_hour_usd', ',.0f')} | "
                      f"{'n/a' if d['cost_per_true_catch_usd'] is None else format(d['cost_per_true_catch_usd'], ',.0f')} | "
                      f"{d['wasted_hours']:.0f} | {d['value_capped_frac']:.3f} | "
                      f"{f('value_capped_per_inv_hour_usd', ',.0f')} |")
        md.append("")
    md.append(f"## Transaction-level ranking metrics (test window, {len(test):,} tx)\n")
    md.append("| System | ROC-AUC | PR-AUC | Note |\n|---|---|---|---|")
    notes = {HELIX_A: "tx score = max Risk_Score of a Case holding it, else 0 (coarse)",
             HELIX_B: "same scores as above", RULE: "rule ratio max(amount/P99, burst/P99)",
             IF_: "unsupervised", XGB: "supervised on the fit window"}
    for sysname in ORDER:
        ra, pa = auc[sysname]["roc_auc"], auc[sysname]["pr_auc"]
        def fr(v):
            v = [x for x in v if x is not None]
            if not v:
                return "n/a"
            return f"{v[0]:.3f}" + (f" [{min(v):.3f}-{max(v):.3f}]" if len(v) > 1 else "")
        md.append(f"| {sysname} | {fr(ra)} | {fr(pa)} | {notes[sysname]} |")
    md.append(f"\nPR-AUC of a random ranking = laundering rate = {y.mean():.5f}.\n")
    md.append(f"## Minutes-per-case sensitivity at N = {cfg['headline_capacity']}\n")
    md.append("| System | " + " | ".join(f"{m} min: value/inv. hour, cost/catch" for m in cfg["minutes_sensitivity"]) + " |")
    md.append("|---|" + "---|" * len(cfg["minutes_sensitivity"]))
    for sysname in ORDER:
        sens = summary["systems"][sysname]["minutes_sensitivity_N%d" % cfg["headline_capacity"]]
        cells = []
        for m in cfg["minutes_sensitivity"]:
            s = sens[str(m)]
            c = "n/a" if s["cost_per_true_catch_usd"] is None else f"{s['cost_per_true_catch_usd']:,.0f}"
            cells.append(f"{s['value_per_inv_hour_usd']:,.0f}, {c}")
        md.append(f"| {sysname} | " + " | ".join(cells) + " |")
    md.append("\n## Auto_Triage (separate HELIX export)\n")
    if triage is None:
        md.append("Not run.")
    else:
        md.append(f"Config: {triage['config']}.\n")
        md.append("| Metric | Value |\n|---|---|")
        for k, v in triage.items():
            if k != "config":
                md.append(f"| {k} | {v:,} |" if isinstance(v, (int, float)) else f"| {k} | {v} |")
    md.append(f"\nRows HELIX cannot ingest (amount outside 0.01 .. 999,999,999.99 after rounding): "
              f"{helix_unseen['rows']} test rows, {helix_unseen['laundering']} laundering, "
              f"{helix_unseen['laundering_usd']:,.0f} USD = {helix_unseen['laundering_value_share']:.1%} of the "
              f"test window's laundering value. The baselines see these rows.")
    (RESULTS / "results.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    # ---- chart: raw value per investigator-hour and the capped variant, N = headline
    hn = str(cfg["headline_capacity"])
    fig, axes = plt.subplots(1, 2, figsize=(13, 5))
    labels = [s.replace(" (", "\n(") for s in ORDER]
    colors = ["#2b6cb0", "#63b3ed", "#a0aec0", "#ed8936", "#38a169"]
    for ax, col, title in ((axes[0], "value_per_inv_hour_usd", "Raw laundering value"),
                           (axes[1], "value_capped_per_inv_hour_usd",
                            f"Value capped at {cfg['value_cap_usd']:,} USD per transaction")):
        vals, errs = [], []
        for s in ORDER:
            d = summary["systems"][s]["by_capacity"][hn]
            v = d[col]
            vals.append(v)
            rg = (d["seed_range"] or {}).get(col)
            errs.append((v - rg[0], rg[1] - v) if rg else (0, 0))
        ax.bar(labels, vals, yerr=np.array(errs).T, capsize=4, color=colors)
        for i, v in enumerate(vals):
            ax.text(i, v, f"{v:,.0f}", ha="center", va="bottom", fontsize=8)
        ax.set_title(title, fontsize=10)
        ax.set_ylabel("USD captured per investigator-hour")
        ax.tick_params(axis="x", labelsize=7)
    fig.suptitle(f"AMLworld HI-Small test window {w['test_start']} .. {w['test_end']} UTC, N = {hn} alerts/hour, "
                 f"{base_min} min/case (error bars: seeds {cfg['seeds']}; AMLworld test data only)", fontsize=10)
    fig.tight_layout()
    fig.savefig(RESULTS / f"value_per_hour_N{hn}.png", dpi=120)
    print((RESULTS / "results.md").read_text(encoding="utf-8"))
    print(f"done in {time.monotonic() - t0:.0f} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
