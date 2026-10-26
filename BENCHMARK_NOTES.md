# AMLworld benchmark notes

Method, assumptions and caveats for `benchmarks/` (code and commands: [benchmarks/README.md](benchmarks/README.md);
full tables: [benchmarks/results/results.md](benchmarks/results/results.md); machine-readable:
`benchmarks/results/summary.json`). Every number is measured on AMLworld HI-Small test data only and is
not a real-world savings estimate.

## Data, windows and row counts

Source: IBM AMLworld HI-Small (`data/HI-Small_Trans.csv`). `prepare_data.py` extracts
[2022/09/03 00:00, 2022/09/04 13:00) once with DuckDB, sorted by (timestamp, original file row), into
`benchmarks/.cache/window.csv`. HELIX and the baselines read that one file, so they see the same rows and
HELIX tx ids (`aml-window-<row>`) join to it by row number. Timestamps are naive and treated as UTC; all
windows are end-exclusive.

| Window | Range | Rows | Laundering rows |
|---|---|---|---|
| Baseline fit | 2022/09/03 00:00 .. 2022/09/04 01:00 | 230,114 | 412 |
| HELIX warm-up (Warm_Up_Mode: builds account memory, opens no Cases) | 2022/09/03 19:00 .. 2022/09/04 01:00 | 62,973 | (not used) |
| Test (every system scored here) | 2022/09/04 01:00 .. 2022/09/04 13:00 (12 h) | 95,881 | 203 (1,104,654,627 USD) |
| Test rows HELIX ingested | same | 95,451 | |

HELIX run (`results/helix_alerts.run.json`): default `HelixConfig`, 5,336 Cases created, 307 ever admitted
to the Active_Queue, 0 worker errors, test replay 455 s.

## Alert unit, capacity and true positives

- One alert = one account investigation. Baseline transactions flagged in the same clock hour are grouped by
  source account; the alert score is the highest transaction score. Isolation Forest and XGBoost flag every
  row, so every account-hour (41,865) is a candidate; the rule flags 662 account-hours.
- A HELIX alert is one Case, placed in the clock hour it was admitted to the Active_Queue. Only the Case's
  test-window transactions up to the end of that hour count (what an investigator could see then). Merged-away
  Cases are dropped.
- Capacity: each system takes its own top N alerts per clock hour (N = 25, 50, 100) by its own score
  (HELIX: Case_Priority).
- True positive: the alert holds at least one laundering transaction. Value captured is the USD sum of the
  distinct laundering transactions inside true-positive alerts.

## Costs

20 minutes per case at 30 USD/h analyst cost = 10 USD per case. Investigator-hours = alerts x 20 / 60.
Cost per true catch = alerts x cost per case / TP. The 10/20/30-minute sensitivity uses the same formula
(5, 10 and 15 USD per case). An earlier scoring run charged 20 USD per alert (a per-case fee on top of the
analyst time), doubling every cost per true catch; that is fixed and covered by a unit test.

## Currency

`Amount Paid` x a fixed, rounded USD rate table (circa 2022) in `benchmarks/config.json`, applied identically to
every system and used only to weight laundering value: USD 1.0, EUR 1.0, GBP 1.17, CHF 1.04, CAD 0.76,
AUD 0.68, JPY 0.0071, CNY 0.145, INR 0.0125, RUB 0.0165, MXN 0.05, BRL 0.19, ILS 0.29, SAR 0.27,
Bitcoin 20,000. These are approximations, not market rates for each timestamp.

## HELIX budget and the Backlog variant

HELIX admits at most 20 Cases per hour to its Active_Queue (`alert_budget_per_period`, default). Over 12 h it
produced 239 Active_Queue alerts, so at N = 25, 50 and 100 it leaves capacity unused, and the
"HELIX (Active_Queue)" row is identical at every N. "HELIX + Backlog (equal capacity)" fills the same N per hour
by also taking never-admitted Backlog Cases (in the hour they were created) by Case_Priority. That variant is a
benchmark construct for equal capacity, not how HELIX runs by default.

## The value cap

Raw laundering value is dominated by two test transactions: one of about 943M USD and one of about 158M USD
hold about 99.8% of the 1.10B USD. Raw value per investigator-hour therefore mostly says which system alerted
on those two accounts. The secondary metric caps each laundering transaction at 100,000 USD.

The 943M USD transaction is one of 430 test rows (85.4% of the laundering value) whose amount, rounded to
cents, is outside HELIX's Transaction_Event range 0.01 .. 999,999,999.99 (large amounts in low-value
currencies and tiny Bitcoin amounts). HELIX rejects these rows at ingest; the baselines see them. This is why
the rule baseline (amount >= fit-window P99) captures 99.7-99.8% of raw value and HELIX at most 14.3%.

## Baselines

Features (causal, past rows only): log USD amount, hour of day, source-account count in the past 1 h and 24 h,
destination new for the source, payment format. Rule: amount >= fit P99 (1,288,300 USD) or 1 h source burst >=
fit P99 (317). Isolation Forest: 200 trees. XGBoost: 200 trees, depth 6, learning rate 0.1, subsample 0.8,
`scale_pos_weight` 557.5, trained on the fit window. No tuning for any system. Isolation Forest and XGBoost run
with seeds 7, 11 and 13; seed 7 is the headline and the range is reported.

## Limitations

- One 12 h test window on one day; no repeated windows or confidence intervals beyond the seed range.
- HELIX runs single-process (about 210 events/s in the test replay); the window was sized to fit that.
- HELIX is untuned: default config and detectors built for card-style fraud. Five of eight AMLworld pattern
  types have no HELIX motif.
- HELIX ROC-AUC / PR-AUC are coarse: a transaction's score is the highest Risk_Score of a Case holding it, else 0.
  Most transactions sit in no Case, so the ranking is mostly ties.
- XGBoost is supervised on fit-window labels. HELIX, the rule and Isolation Forest never see labels, so XGBoost
  is an upper reference, not a like-for-like competitor.
- Auto_Triage not run: no consistent `helix_alerts_triage.csv` export exists for this window, so no auto-cleared
  Case counts, hours saved or laundering-inside numbers are reported.

## Who won where (AMLworld test data)

- **Overall: XGBoost (supervised).** At every N it has the most true positives (82 / 90 / 106 vs at most 25 for
  any label-free system), the highest alert precision and recall, the lowest cost per true catch (37 / 67 / 113
  USD) and the highest capped value per investigator-hour, plus ROC-AUC 0.969 and PR-AUC 0.335. It misses both
  huge transactions, so its raw value share is 0.1%.
- **Raw value: the rule.** It alerts on both huge transactions (including the 943M USD one HELIX cannot ingest),
  so it leads raw value captured and raw value per investigator-hour at every N.
- **Label-free, counts and cost:** HELIX leads. HELIX (Active_Queue) has 11 TP from 239 alerts (precision 0.046,
  cost per true catch 217 USD, 76 wasted hours) against Isolation Forest 10-11 TP / 300 USD / 96.7 wasted hours at
  N = 25 and the rule 3-4 TP / 1,000+ USD / 99+ wasted hours. At equal capacity, HELIX + Backlog has more TP (12 / 16 / 25) and recall (0.064 / 0.089 / 0.138) than the
  rule and Isolation Forest (at N = 25 recall ties Isolation Forest's best seed), with lower cost per true catch.
- **Label-free, capped value per investigator-hour:** HELIX leads at N = 25 (Active_Queue 3,492 and Backlog
  2,796 vs rule 2,002 and Isolation Forest 1,301-1,490 USD/h). At equal capacity for N = 50 and 100 the rule is
  ahead of HELIX + Backlog (1,550 vs 1,438 and 1,360 vs 883 USD/h), because it uses fewer alerts. HELIX
  (Active_Queue) stays highest only because it works 80 hours instead of the full capacity.
- **Label-free, ranking:** HELIX does not lead. Its coarse ROC-AUC is 0.505 and PR-AUC 0.0022, about the random
  level of 0.0021; Isolation Forest has the best label-free ROC-AUC (0.703-0.735) and the rule the best label-free
  PR-AUC (0.005).
