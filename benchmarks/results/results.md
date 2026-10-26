# AMLworld HI-Small benchmark results

Test window 2022/09/04 01:00 .. 2022/09/04 13:00 UTC (12 h, 95,881 tx, 203 laundering, 1,104,654,627 USD laundering value). Baselines fit on 2022/09/03 00:00 .. 2022/09/04 01:00 (230,114 tx). HELIX warm-up 2022/09/03 19:00 .. 2022/09/04 01:00 (62,973 tx, Warm_Up_Mode), default config.
Costs: 20 min/case x 30 USD/h analyst = 10 USD/case; cost per true catch = alerts x cost per case / TP. IF/XGBoost: seed 7 shown, range over seeds [7, 11, 13] in brackets. On AMLworld test data only.

## N = 25 alerts per hour (capacity 300)

| System | Alerts | TP | FP | Alert precision | Precision@N | Tx recall | Value captured (USD) | Share of laundering value | Inv. hours | Value / inv. hour (USD) | Cost per true catch (USD) | Wasted hours | Capped value share (cap 100,000 USD/tx) | Capped value / inv. hour (USD) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| HELIX (Active_Queue, own budget 20/h) | 239 | 11 | 228 | 0.046 | 0.037 | 0.059 | 158,019,634 | 0.143 | 80 | 1,983,510 | 217 | 76 | 0.140 | 3,492 |
| HELIX + Backlog (equal capacity) | 300 | 12 | 288 | 0.040 | 0.040 | 0.064 | 158,021,085 | 0.143 | 100 | 1,580,211 | 250 | 96 | 0.140 | 2,796 |
| Rule (P99 amount / 1h burst) | 300 | 3 | 297 | 0.010 | 0.010 | 0.015 | 1,100,869,352 | 0.997 | 100 | 11,008,694 | 1,000 | 99 | 0.100 | 2,002 |
| Isolation Forest | 300 | 10 [10-11] | 290 | 0.033 [0.033-0.037] | 0.033 | 0.059 [0.059-0.064] | 157,871,585 [157,871,585-157,890,412] | 0.143 | 100 | 1,578,716 [1,578,716-1,578,904] | 300 | 97 | 0.065 | 1,301 [1,301-1,490] |
| XGBoost (supervised) | 300 | 82 [80-83] | 218 | 0.273 [0.267-0.277] | 0.273 | 0.414 [0.404-0.419] | 792,102 [775,706-792,102] | 0.001 | 100 | 7,921 [7,757-7,921] | 37 | 73 | 0.397 | 7,921 [7,757-7,921] |

## N = 50 alerts per hour (capacity 600)

| System | Alerts | TP | FP | Alert precision | Precision@N | Tx recall | Value captured (USD) | Share of laundering value | Inv. hours | Value / inv. hour (USD) | Cost per true catch (USD) | Wasted hours | Capped value share (cap 100,000 USD/tx) | Capped value / inv. hour (USD) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| HELIX (Active_Queue, own budget 20/h) | 239 | 11 | 228 | 0.046 | 0.018 | 0.059 | 158,019,634 | 0.143 | 80 | 1,983,510 | 217 | 76 | 0.140 | 3,492 |
| HELIX + Backlog (equal capacity) | 600 | 16 | 584 | 0.027 | 0.027 | 0.089 | 158,028,949 | 0.143 | 200 | 790,145 | 375 | 195 | 0.144 | 1,438 |
| Rule (P99 amount / 1h burst) | 581 | 4 | 577 | 0.007 | 0.007 | 0.020 | 1,102,961,375 | 0.998 | 194 | 5,695,153 | 1,452 | 192 | 0.151 | 1,550 |
| Isolation Forest | 600 | 11 [11-12] | 589 | 0.018 [0.018-0.020] | 0.018 | 0.064 [0.064-0.069] | 157,890,412 [157,890,412-1,100,918,111] | 0.143 | 200 | 789,452 [789,452-5,504,591] | 545 | 196 | 0.075 | 745 [745-1,245] |
| XGBoost (supervised) | 600 | 90 [90-93] | 510 | 0.150 [0.150-0.155] | 0.150 | 0.453 [0.453-0.468] | 864,341 [864,341-889,352] | 0.001 | 200 | 4,322 [4,322-4,447] | 67 | 170 | 0.434 | 4,322 [4,322-4,447] |

## N = 100 alerts per hour (capacity 1200)

| System | Alerts | TP | FP | Alert precision | Precision@N | Tx recall | Value captured (USD) | Share of laundering value | Inv. hours | Value / inv. hour (USD) | Cost per true catch (USD) | Wasted hours | Capped value share (cap 100,000 USD/tx) | Capped value / inv. hour (USD) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| HELIX (Active_Queue, own budget 20/h) | 239 | 11 | 228 | 0.046 | 0.009 | 0.059 | 158,019,634 | 0.143 | 80 | 1,983,510 | 217 | 76 | 0.140 | 3,492 |
| HELIX + Backlog (equal capacity) | 1200 | 25 | 1175 | 0.021 | 0.021 | 0.138 | 158,094,800 | 0.143 | 400 | 395,237 | 480 | 392 | 0.177 | 883 |
| Rule (P99 amount / 1h burst) | 662 | 4 | 658 | 0.006 | 0.003 | 0.020 | 1,102,961,375 | 0.998 | 221 | 4,998,314 | 1,655 | 219 | 0.151 | 1,360 |
| Isolation Forest | 1200 | 11 [11-14] | 1189 | 0.009 [0.009-0.012] | 0.009 | 0.064 [0.064-0.079] | 157,890,412 [157,890,412-1,103,011,057] | 0.143 | 400 | 394,726 [394,726-2,757,528] | 1,091 | 396 | 0.075 | 372 [372-875] |
| XGBoost (supervised) | 1200 | 106 [105-109] | 1094 | 0.088 [0.087-0.091] | 0.088 | 0.532 [0.527-0.547] | 987,740 [987,740-1,006,304] | 0.001 | 400 | 2,469 [2,469-2,516] | 113 | 365 | 0.495 | 2,469 [2,469-2,516] |

## Transaction-level ranking metrics (test window, 95,881 tx)

| System | ROC-AUC | PR-AUC | Note |
|---|---|---|---|
| HELIX (Active_Queue, own budget 20/h) | 0.505 | 0.002 | tx score = max Risk_Score of a Case holding it, else 0 (coarse) |
| HELIX + Backlog (equal capacity) | 0.505 | 0.002 | same scores as above |
| Rule (P99 amount / 1h burst) | 0.521 | 0.005 | rule ratio max(amount/P99, burst/P99) |
| Isolation Forest | 0.735 [0.703-0.735] | 0.004 [0.003-0.004] | unsupervised |
| XGBoost (supervised) | 0.969 [0.967-0.971] | 0.335 [0.335-0.335] | supervised on the fit window |

PR-AUC of a random ranking = laundering rate = 0.00212.

## Minutes-per-case sensitivity at N = 50

| System | 10 min: value/inv. hour, cost/catch | 20 min: value/inv. hour, cost/catch | 30 min: value/inv. hour, cost/catch |
|---|---|---|---|
| HELIX (Active_Queue, own budget 20/h) | 3,967,020, 109 | 1,983,510, 217 | 1,322,340, 326 |
| HELIX + Backlog (equal capacity) | 1,580,289, 188 | 790,145, 375 | 526,763, 562 |
| Rule (P99 amount / 1h burst) | 11,390,307, 726 | 5,695,153, 1,452 | 3,796,769, 2,179 |
| Isolation Forest | 1,578,904, 273 | 789,452, 545 | 526,301, 818 |
| XGBoost (supervised) | 8,643, 33 | 4,322, 67 | 2,881, 100 |

## Auto_Triage (separate HELIX export)

Not run.

Rows HELIX cannot ingest (amount outside 0.01 .. 999,999,999.99 after rounding): 430 test rows, 1 laundering, 943,027,699 USD = 85.4% of the test window's laundering value. The baselines see these rows.
