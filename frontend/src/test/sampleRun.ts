// Real API responses captured from the backend sample run (docs/sample_transactions.jsonl,
// HelixConfig(sensors=SensorConfig(min_history_count=5)), the same setup as
// backend/tests/integration/test_api.py). Regenerate rather than hand-edit.
/* eslint-disable */
import type { CaseDetail, EvidenceDoc } from "../api/types";

/** GET /api/v1/cases/C-1 - Alice's T007 (EUR 4,800, Berlin to Lagos, unknown payee). */
export const aliceDetail: CaseDetail = {
  "case_id": "C-1",
  "status": "OPEN",
  "tier": "CRITICAL",
  "risk": 0.9732835137936607,
  "uncertainty": 0.04926343807255775,
  "priority": "4621.76",
  "expected_loss": "4671.76",
  "placement": "ACTIVE",
  "amount_at_risk": [
    {
      "currency": "EUR",
      "amount": "4800.00"
    }
  ],
  "accounts": [
    "ACC-ALICE",
    "ACC-UNKNOWN-77"
  ],
  "tx_count": 1,
  "motifs": [],
  "assignee": null,
  "merged_into": null,
  "label": null,
  "updated_at": "2024-06-05T11:20:00+00:00",
  "seq_no": 1,
  "tx_ids": [
    "T007"
  ],
  "created_at": "2024-06-05T11:20:00+00:00",
  "history": [
    {
      "ts": "2024-06-05T11:20:00+00:00",
      "action": "create",
      "prev": null,
      "new": "OPEN",
      "actor": "system"
    },
    {
      "ts": "2024-06-05T11:20:00+00:00",
      "action": "placement",
      "prev": "NONE",
      "new": "ACTIVE",
      "actor": "system"
    }
  ],
  "risk_at_creation": 0.9732835137936607,
  "escalated": false,
  "escalated_by": null,
  "opened_at": null,
  "label_by": null,
  "label_at": null,
  "label_note": null,
  "admitted_period": 83,
  "evidence_available": true
};

/** GET /api/v1/cases/C-1/evidence */
export const aliceEvidence: EvidenceDoc = {
  "case_id": "C-1",
  "chain": {
    "case_id": "C-1",
    "entries": [
      {
        "contributor_id": "amount",
        "ctype": "SENSOR",
        "detail": {
          "outcome": "SUCCESS",
          "vacuous_reason": null
        },
        "features": {
          "amount": 4800.0,
          "mean": 37.68625498095482,
          "std": 13.738401550764642,
          "z": 346.6424916626482
        },
        "kind": "CONTRIBUTOR",
        "mass": {
          "f": 0.7,
          "l": 0.0,
          "t": 0.30000000000000004
        },
        "ts": "2024-06-05T11:20:00+00:00",
        "tx_id": "T007",
        "weight": 1.0
      },
      {
        "contributor_id": "counterparty",
        "ctype": "SENSOR",
        "detail": {
          "outcome": "SUCCESS",
          "vacuous_reason": null
        },
        "features": {
          "dst": "ACC-UNKNOWN-77",
          "known": 0.0,
          "n_counterparties": 3.0
        },
        "kind": "CONTRIBUTOR",
        "mass": {
          "f": 0.35,
          "l": 0.0,
          "t": 0.65
        },
        "ts": "2024-06-05T11:20:00+00:00",
        "tx_id": "T007",
        "weight": 1.0
      },
      {
        "contributor_id": "drift",
        "ctype": "SENSOR",
        "detail": {
          "outcome": "SUCCESS",
          "vacuous_reason": null
        },
        "features": {
          "candidate_tx": null,
          "cpd_kind": "NONE"
        },
        "kind": "CONTRIBUTOR",
        "mass": {
          "f": 0.0,
          "l": 0.0,
          "t": 1.0
        },
        "ts": "2024-06-05T11:20:00+00:00",
        "tx_id": "T007",
        "weight": 1.0
      },
      {
        "contributor_id": "geo",
        "ctype": "SENSOR",
        "detail": {
          "outcome": "SUCCESS",
          "vacuous_reason": null
        },
        "features": {
          "distance_km": 5196.145796050512,
          "dt_h": 1.1666666666666667,
          "speed_kmh": 4453.839253757582,
          "vmax_kmh": 900.0
        },
        "kind": "CONTRIBUTOR",
        "mass": {
          "f": 0.638341718464007,
          "l": 0.0,
          "t": 0.361658281535993
        },
        "ts": "2024-06-05T11:20:00+00:00",
        "tx_id": "T007",
        "weight": 1.0
      },
      {
        "contributor_id": "timing",
        "ctype": "SENSOR",
        "detail": {
          "outcome": "SUCCESS",
          "vacuous_reason": null
        },
        "features": {
          "freq_hour": 0.0,
          "freq_weekday": 0.34058123502698434,
          "hour": 11.0,
          "p": 0.0,
          "weekday": 2.0
        },
        "kind": "CONTRIBUTOR",
        "mass": {
          "f": 0.3,
          "l": 0.0,
          "t": 0.7
        },
        "ts": "2024-06-05T11:20:00+00:00",
        "tx_id": "T007",
        "weight": 1.0
      },
      {
        "contributor_id": "velocity",
        "ctype": "SENSOR",
        "detail": {
          "outcome": "SUCCESS",
          "vacuous_reason": null
        },
        "features": {
          "count_1h": 2.0,
          "count_1m": 1.0,
          "count_24h": 4.0,
          "expected_1h": 1.0,
          "expected_1m": 1.0,
          "expected_24h": 2.4273947343163984,
          "mean_iat_s": 35593.71649717775,
          "ratio": 2.0
        },
        "kind": "CONTRIBUTOR",
        "mass": {
          "f": 0.0,
          "l": 0.04060058497098381,
          "t": 0.9593994150290162
        },
        "ts": "2024-06-05T11:20:00+00:00",
        "tx_id": "T007",
        "weight": 1.0
      },
      {
        "contributor_id": "~cp",
        "ctype": null,
        "detail": {
          "classification": "ABRUPT_BREAK",
          "est_start_tx": "T007",
          "short_run_mass": 0.9294066562760456
        },
        "features": {},
        "kind": "CHANGE_POINT",
        "mass": null,
        "ts": "2024-06-05T11:20:00+00:00",
        "tx_id": "T007",
        "weight": null
      },
      {
        "contributor_id": "~fused",
        "ctype": null,
        "detail": {
          "amount": {
            "$decimal": "4800.00"
          },
          "belief": 0.9486517947573818,
          "conflict": 0.03859628206265398,
          "currency": "EUR",
          "hc_threshold": 0.95,
          "high_conflict": false,
          "plausibility": 0.9979152328299395,
          "stage": 1,
          "uncertainty": 0.04926343807255775
        },
        "features": {},
        "kind": "FUSED",
        "mass": {
          "f": 0.9486517947573818,
          "l": 0.0020847671700604366,
          "t": 0.04926343807255775
        },
        "ts": "2024-06-05T11:20:00+00:00",
        "tx_id": "T007",
        "weight": null
      }
    ],
    "schema": 1
  },
  "explanation": {
    "case_id": "C-1",
    "summary": "CRITICAL | risk 0.973 | Bel 0.949 Pl 0.998 U 0.049 K 0.039 | at risk 4800.00 EUR",
    "reasons": [
      {
        "name": "amount",
        "label": "Amount",
        "ctype": "SENSOR",
        "weight": 1.0,
        "tx_ids": [
          "T007"
        ],
        "f": 0.7,
        "l": 0.0,
        "comparison": "amount 4800.00 vs baseline mean 37.69 (sd 13.74, z 346.64)",
        "generic": false,
        "text": "Amount: amount 4800.00 vs baseline mean 37.69 (sd 13.74, z 346.64) | weight 1.00, F' 0.700 vs L' 0.000, tx T007"
      },
      {
        "name": "geo",
        "label": "Geo",
        "ctype": "SENSOR",
        "weight": 1.0,
        "tx_ids": [
          "T007"
        ],
        "f": 0.638341718464007,
        "l": 0.0,
        "comparison": "5196.1 km in 1.17 h = 4454 km/h vs plausible 900 km/h",
        "generic": false,
        "text": "Geo: 5196.1 km in 1.17 h = 4454 km/h vs plausible 900 km/h | weight 1.00, F' 0.638 vs L' 0.000, tx T007"
      },
      {
        "name": "counterparty",
        "label": "Counterparty",
        "ctype": "SENSOR",
        "weight": 1.0,
        "tx_ids": [
          "T007"
        ],
        "f": 0.35,
        "l": 0.0,
        "comparison": "payee ACC-UNKNOWN-77 not seen before (baseline 3 known counterparties)",
        "generic": false,
        "text": "Counterparty: payee ACC-UNKNOWN-77 not seen before (baseline 3 known counterparties) | weight 1.00, F' 0.350 vs L' 0.000, tx T007"
      },
      {
        "name": "timing",
        "label": "Timing",
        "ctype": "SENSOR",
        "weight": 1.0,
        "tx_ids": [
          "T007"
        ],
        "f": 0.3,
        "l": 0.0,
        "comparison": "11:00 UTC on Wed vs baseline frequency hour 0.000, weekday 0.341 (p 0.0000)",
        "generic": false,
        "text": "Timing: 11:00 UTC on Wed vs baseline frequency hour 0.000, weekday 0.341 (p 0.0000) | weight 1.00, F' 0.300 vs L' 0.000, tx T007"
      }
    ],
    "mitigating": [
      {
        "name": "velocity",
        "label": "Velocity",
        "ctype": "SENSOR",
        "weight": 1.0,
        "tx_ids": [
          "T007"
        ],
        "f": 0.0,
        "l": 0.04060058497098381,
        "comparison": "events 1h 2 vs expected 1.00; 24h 4 vs expected 2.43 (ratio 2.00)",
        "generic": false,
        "text": "Velocity: events 1h 2 vs expected 1.00; 24h 4 vs expected 2.43 (ratio 2.00) | weight 1.00, F' 0.000 vs L' 0.041, tx T007"
      }
    ],
    "insufficient": [
      {
        "name": "drift",
        "label": "Drift",
        "ctype": "SENSOR",
        "weight": 1.0,
        "tx_ids": [
          "T007"
        ],
        "f": 0.0,
        "l": 0.0,
        "comparison": "no decisive evidence (F' = L')",
        "generic": false,
        "text": "Drift: no decisive evidence (F' = L') | weight 1.00, F' 0.000 vs L' 0.000, tx T007"
      }
    ],
    "notes": [
      "T007: change point ABRUPT_BREAK"
    ],
    "message": null,
    "text": "CRITICAL | risk 0.973 | Bel 0.949 Pl 0.998 U 0.049 K 0.039 | at risk 4800.00 EUR\nReasons (4):\n  1. Amount: amount 4800.00 vs baseline mean 37.69 (sd 13.74, z 346.64) | weight 1.00, F' 0.700 vs L' 0.000, tx T007\n  2. Geo: 5196.1 km in 1.17 h = 4454 km/h vs plausible 900 km/h | weight 1.00, F' 0.638 vs L' 0.000, tx T007\n  3. Counterparty: payee ACC-UNKNOWN-77 not seen before (baseline 3 known counterparties) | weight 1.00, F' 0.350 vs L' 0.000, tx T007\n  4. Timing: 11:00 UTC on Wed vs baseline frequency hour 0.000, weekday 0.341 (p 0.0000) | weight 1.00, F' 0.300 vs L' 0.000, tx T007\nMitigating (1):\n  - Velocity: events 1h 2 vs expected 1.00; 24h 4 vs expected 2.43 (ratio 2.00) | weight 1.00, F' 0.000 vs L' 0.041, tx T007\nInsufficient evidence (1):\n  - Drift: no decisive evidence (F' = L') | weight 1.00, F' 0.000 vs L' 0.000, tx T007\nRecords (1):\n  - T007: change point ABRUPT_BREAK"
  }
};

/** GET /api/v1/cases/C-2 - Bob's STRUCTURING case T010-T012 (no location, no history). */
export const bobDetail: CaseDetail = {
  "case_id": "C-2",
  "status": "OPEN",
  "tier": "HIGH",
  "risk": 0.8,
  "uncertainty": 0.4,
  "priority": "23430.00",
  "expected_loss": "23480.00",
  "placement": "ACTIVE",
  "amount_at_risk": [
    {
      "currency": "EUR",
      "amount": "29350.00"
    }
  ],
  "accounts": [
    "ACC-BOB",
    "ACC-SHELL-1",
    "ACC-SHELL-2",
    "ACC-SHELL-3"
  ],
  "tx_count": 3,
  "motifs": [
    "STRUCTURING"
  ],
  "assignee": null,
  "merged_into": null,
  "label": null,
  "updated_at": "2024-06-06T08:45:00+00:00",
  "seq_no": 2,
  "tx_ids": [
    "T010",
    "T011",
    "T012"
  ],
  "created_at": "2024-06-06T08:45:00+00:00",
  "history": [
    {
      "ts": "2024-06-06T08:45:00+00:00",
      "action": "create",
      "prev": null,
      "new": "OPEN",
      "actor": "system"
    },
    {
      "ts": "2024-06-06T08:45:00+00:00",
      "action": "placement",
      "prev": "NONE",
      "new": "ACTIVE",
      "actor": "system"
    }
  ],
  "risk_at_creation": 0.8,
  "escalated": false,
  "escalated_by": null,
  "opened_at": null,
  "label_by": null,
  "label_at": null,
  "label_note": null,
  "admitted_period": 104,
  "evidence_available": true
};

/** GET /api/v1/cases/C-2/evidence */
export const bobEvidence: EvidenceDoc = {
  "case_id": "C-2",
  "chain": {
    "case_id": "C-2",
    "entries": [
      {
        "contributor_id": "amount",
        "ctype": "SENSOR",
        "detail": {
          "outcome": "SUCCESS",
          "vacuous_reason": "history:0"
        },
        "features": {
          "history_count": 0.0
        },
        "kind": "CONTRIBUTOR",
        "mass": {
          "f": 0.0,
          "l": 0.0,
          "t": 1.0
        },
        "ts": "2024-06-05T09:00:00+00:00",
        "tx_id": "T010",
        "weight": 1.0
      },
      {
        "contributor_id": "counterparty",
        "ctype": "SENSOR",
        "detail": {
          "outcome": "SUCCESS",
          "vacuous_reason": "history:0"
        },
        "features": {
          "history_count": 0.0
        },
        "kind": "CONTRIBUTOR",
        "mass": {
          "f": 0.0,
          "l": 0.0,
          "t": 1.0
        },
        "ts": "2024-06-05T09:00:00+00:00",
        "tx_id": "T010",
        "weight": 1.0
      },
      {
        "contributor_id": "drift",
        "ctype": "SENSOR",
        "detail": {
          "outcome": "SUCCESS",
          "vacuous_reason": "history:0"
        },
        "features": {
          "history_count": 0.0
        },
        "kind": "CONTRIBUTOR",
        "mass": {
          "f": 0.0,
          "l": 0.0,
          "t": 1.0
        },
        "ts": "2024-06-05T09:00:00+00:00",
        "tx_id": "T010",
        "weight": 1.0
      },
      {
        "contributor_id": "geo",
        "ctype": "SENSOR",
        "detail": {
          "outcome": "SUCCESS",
          "vacuous_reason": "missing:lat,lon"
        },
        "features": {},
        "kind": "CONTRIBUTOR",
        "mass": {
          "f": 0.0,
          "l": 0.0,
          "t": 1.0
        },
        "ts": "2024-06-05T09:00:00+00:00",
        "tx_id": "T010",
        "weight": 1.0
      },
      {
        "contributor_id": "timing",
        "ctype": "SENSOR",
        "detail": {
          "outcome": "SUCCESS",
          "vacuous_reason": "history:0"
        },
        "features": {
          "history_count": 0.0
        },
        "kind": "CONTRIBUTOR",
        "mass": {
          "f": 0.0,
          "l": 0.0,
          "t": 1.0
        },
        "ts": "2024-06-05T09:00:00+00:00",
        "tx_id": "T010",
        "weight": 1.0
      },
      {
        "contributor_id": "velocity",
        "ctype": "SENSOR",
        "detail": {
          "outcome": "SUCCESS",
          "vacuous_reason": "history:0"
        },
        "features": {
          "history_count": 0.0
        },
        "kind": "CONTRIBUTOR",
        "mass": {
          "f": 0.0,
          "l": 0.0,
          "t": 1.0
        },
        "ts": "2024-06-05T09:00:00+00:00",
        "tx_id": "T010",
        "weight": 1.0
      },
      {
        "contributor_id": "~fused",
        "ctype": null,
        "detail": {
          "amount": {
            "$decimal": "9800.00"
          },
          "belief": 0.0,
          "conflict": 0.0,
          "currency": "EUR",
          "hc_threshold": 0.95,
          "high_conflict": false,
          "plausibility": 1.0,
          "stage": 1,
          "uncertainty": 1.0
        },
        "features": {},
        "kind": "FUSED",
        "mass": {
          "f": 0.0,
          "l": 0.0,
          "t": 1.0
        },
        "ts": "2024-06-05T09:00:00+00:00",
        "tx_id": "T010",
        "weight": null
      },
      {
        "contributor_id": "~missing:lat",
        "ctype": null,
        "detail": {
          "field": "lat",
          "sensors": "geo"
        },
        "features": {},
        "kind": "MISSING_FIELD",
        "mass": null,
        "ts": "2024-06-05T09:00:00+00:00",
        "tx_id": "T010",
        "weight": null
      },
      {
        "contributor_id": "~missing:lon",
        "ctype": null,
        "detail": {
          "field": "lon",
          "sensors": "geo"
        },
        "features": {},
        "kind": "MISSING_FIELD",
        "mass": null,
        "ts": "2024-06-05T09:00:00+00:00",
        "tx_id": "T010",
        "weight": null
      },
      {
        "contributor_id": "~stage2:9f18c79ae465",
        "ctype": null,
        "detail": {
          "amount": null,
          "belief": 0.6,
          "conflict": 0.0,
          "currency": null,
          "hc_threshold": 0.95,
          "high_conflict": false,
          "motif_weights": "1.0",
          "motifs": "STRUCTURING:3e24d8282e37dec5",
          "plausibility": 1.0,
          "seed_f": 0.0,
          "seed_l": 0.0,
          "seed_t": 1.0,
          "seed_tx": "T010",
          "stage": 2,
          "uncertainty": 0.4
        },
        "features": {},
        "kind": "FUSED",
        "mass": {
          "f": 0.6,
          "l": 0.0,
          "t": 0.4
        },
        "ts": "2024-06-05T09:00:00+00:00",
        "tx_id": "T010",
        "weight": null
      },
      {
        "contributor_id": "amount",
        "ctype": "SENSOR",
        "detail": {
          "outcome": "SUCCESS",
          "vacuous_reason": "history:1"
        },
        "features": {
          "history_count": 1.0
        },
        "kind": "CONTRIBUTOR",
        "mass": {
          "f": 0.0,
          "l": 0.0,
          "t": 1.0
        },
        "ts": "2024-06-05T15:30:00+00:00",
        "tx_id": "T011",
        "weight": 1.0
      },
      {
        "contributor_id": "counterparty",
        "ctype": "SENSOR",
        "detail": {
          "outcome": "SUCCESS",
          "vacuous_reason": "history:1"
        },
        "features": {
          "history_count": 1.0
        },
        "kind": "CONTRIBUTOR",
        "mass": {
          "f": 0.0,
          "l": 0.0,
          "t": 1.0
        },
        "ts": "2024-06-05T15:30:00+00:00",
        "tx_id": "T011",
        "weight": 1.0
      },
      {
        "contributor_id": "drift",
        "ctype": "SENSOR",
        "detail": {
          "outcome": "SUCCESS",
          "vacuous_reason": "history:1"
        },
        "features": {
          "history_count": 1.0
        },
        "kind": "CONTRIBUTOR",
        "mass": {
          "f": 0.0,
          "l": 0.0,
          "t": 1.0
        },
        "ts": "2024-06-05T15:30:00+00:00",
        "tx_id": "T011",
        "weight": 1.0
      },
      {
        "contributor_id": "geo",
        "ctype": "SENSOR",
        "detail": {
          "outcome": "SUCCESS",
          "vacuous_reason": "missing:lat,lon"
        },
        "features": {},
        "kind": "CONTRIBUTOR",
        "mass": {
          "f": 0.0,
          "l": 0.0,
          "t": 1.0
        },
        "ts": "2024-06-05T15:30:00+00:00",
        "tx_id": "T011",
        "weight": 1.0
      },
      {
        "contributor_id": "timing",
        "ctype": "SENSOR",
        "detail": {
          "outcome": "SUCCESS",
          "vacuous_reason": "history:1"
        },
        "features": {
          "history_count": 1.0
        },
        "kind": "CONTRIBUTOR",
        "mass": {
          "f": 0.0,
          "l": 0.0,
          "t": 1.0
        },
        "ts": "2024-06-05T15:30:00+00:00",
        "tx_id": "T011",
        "weight": 1.0
      },
      {
        "contributor_id": "velocity",
        "ctype": "SENSOR",
        "detail": {
          "outcome": "SUCCESS",
          "vacuous_reason": "history:1"
        },
        "features": {
          "history_count": 1.0
        },
        "kind": "CONTRIBUTOR",
        "mass": {
          "f": 0.0,
          "l": 0.0,
          "t": 1.0
        },
        "ts": "2024-06-05T15:30:00+00:00",
        "tx_id": "T011",
        "weight": 1.0
      },
      {
        "contributor_id": "~fused",
        "ctype": null,
        "detail": {
          "amount": {
            "$decimal": "9650.00"
          },
          "belief": 0.0,
          "conflict": 0.0,
          "currency": "EUR",
          "hc_threshold": 0.95,
          "high_conflict": false,
          "plausibility": 1.0,
          "stage": 1,
          "uncertainty": 1.0
        },
        "features": {},
        "kind": "FUSED",
        "mass": {
          "f": 0.0,
          "l": 0.0,
          "t": 1.0
        },
        "ts": "2024-06-05T15:30:00+00:00",
        "tx_id": "T011",
        "weight": null
      },
      {
        "contributor_id": "~missing:lat",
        "ctype": null,
        "detail": {
          "field": "lat",
          "sensors": "geo"
        },
        "features": {},
        "kind": "MISSING_FIELD",
        "mass": null,
        "ts": "2024-06-05T15:30:00+00:00",
        "tx_id": "T011",
        "weight": null
      },
      {
        "contributor_id": "~missing:lon",
        "ctype": null,
        "detail": {
          "field": "lon",
          "sensors": "geo"
        },
        "features": {},
        "kind": "MISSING_FIELD",
        "mass": null,
        "ts": "2024-06-05T15:30:00+00:00",
        "tx_id": "T011",
        "weight": null
      },
      {
        "contributor_id": "STRUCTURING:3e24d8282e37dec5",
        "ctype": "MOTIF",
        "detail": {
          "fuser_id": "motif:STRUCTURING",
          "truncated": false,
          "ts_end": "2024-06-06T08:45:00+00:00",
          "ts_start": "2024-06-05T09:00:00+00:00"
        },
        "features": {
          "accounts": "ACC-BOB",
          "detail.reporting_threshold": "10000",
          "duration_s": 85500.0,
          "motif_type": "STRUCTURING",
          "n_tx": 3,
          "tx_ids": "T010|T011|T012"
        },
        "kind": "MOTIF",
        "mass": {
          "f": 0.6,
          "l": 0.0,
          "t": 0.4
        },
        "ts": "2024-06-06T08:45:00+00:00",
        "tx_id": "T012",
        "weight": 1.0
      },
      {
        "contributor_id": "amount",
        "ctype": "SENSOR",
        "detail": {
          "outcome": "SUCCESS",
          "vacuous_reason": "history:2"
        },
        "features": {
          "history_count": 2.0
        },
        "kind": "CONTRIBUTOR",
        "mass": {
          "f": 0.0,
          "l": 0.0,
          "t": 1.0
        },
        "ts": "2024-06-06T08:45:00+00:00",
        "tx_id": "T012",
        "weight": 1.0
      },
      {
        "contributor_id": "counterparty",
        "ctype": "SENSOR",
        "detail": {
          "outcome": "SUCCESS",
          "vacuous_reason": "history:2"
        },
        "features": {
          "history_count": 2.0
        },
        "kind": "CONTRIBUTOR",
        "mass": {
          "f": 0.0,
          "l": 0.0,
          "t": 1.0
        },
        "ts": "2024-06-06T08:45:00+00:00",
        "tx_id": "T012",
        "weight": 1.0
      },
      {
        "contributor_id": "drift",
        "ctype": "SENSOR",
        "detail": {
          "outcome": "SUCCESS",
          "vacuous_reason": "history:2"
        },
        "features": {
          "history_count": 2.0
        },
        "kind": "CONTRIBUTOR",
        "mass": {
          "f": 0.0,
          "l": 0.0,
          "t": 1.0
        },
        "ts": "2024-06-06T08:45:00+00:00",
        "tx_id": "T012",
        "weight": 1.0
      },
      {
        "contributor_id": "geo",
        "ctype": "SENSOR",
        "detail": {
          "outcome": "SUCCESS",
          "vacuous_reason": "missing:lat,lon"
        },
        "features": {},
        "kind": "CONTRIBUTOR",
        "mass": {
          "f": 0.0,
          "l": 0.0,
          "t": 1.0
        },
        "ts": "2024-06-06T08:45:00+00:00",
        "tx_id": "T012",
        "weight": 1.0
      },
      {
        "contributor_id": "timing",
        "ctype": "SENSOR",
        "detail": {
          "outcome": "SUCCESS",
          "vacuous_reason": "history:2"
        },
        "features": {
          "history_count": 2.0
        },
        "kind": "CONTRIBUTOR",
        "mass": {
          "f": 0.0,
          "l": 0.0,
          "t": 1.0
        },
        "ts": "2024-06-06T08:45:00+00:00",
        "tx_id": "T012",
        "weight": 1.0
      },
      {
        "contributor_id": "velocity",
        "ctype": "SENSOR",
        "detail": {
          "outcome": "SUCCESS",
          "vacuous_reason": "history:2"
        },
        "features": {
          "history_count": 2.0
        },
        "kind": "CONTRIBUTOR",
        "mass": {
          "f": 0.0,
          "l": 0.0,
          "t": 1.0
        },
        "ts": "2024-06-06T08:45:00+00:00",
        "tx_id": "T012",
        "weight": 1.0
      },
      {
        "contributor_id": "~fused",
        "ctype": null,
        "detail": {
          "amount": {
            "$decimal": "9900.00"
          },
          "belief": 0.0,
          "conflict": 0.0,
          "currency": "EUR",
          "hc_threshold": 0.95,
          "high_conflict": false,
          "plausibility": 1.0,
          "stage": 1,
          "uncertainty": 1.0
        },
        "features": {},
        "kind": "FUSED",
        "mass": {
          "f": 0.0,
          "l": 0.0,
          "t": 1.0
        },
        "ts": "2024-06-06T08:45:00+00:00",
        "tx_id": "T012",
        "weight": null
      },
      {
        "contributor_id": "~missing:lat",
        "ctype": null,
        "detail": {
          "field": "lat",
          "sensors": "geo"
        },
        "features": {},
        "kind": "MISSING_FIELD",
        "mass": null,
        "ts": "2024-06-06T08:45:00+00:00",
        "tx_id": "T012",
        "weight": null
      },
      {
        "contributor_id": "~missing:lon",
        "ctype": null,
        "detail": {
          "field": "lon",
          "sensors": "geo"
        },
        "features": {},
        "kind": "MISSING_FIELD",
        "mass": null,
        "ts": "2024-06-06T08:45:00+00:00",
        "tx_id": "T012",
        "weight": null
      }
    ],
    "schema": 1
  },
  "explanation": {
    "case_id": "C-2",
    "summary": "HIGH | risk 0.800 | Bel 0.600 Pl 1.000 U 0.400 K 0.000 | at risk 29350.00 EUR",
    "reasons": [
      {
        "name": "STRUCTURING:3e24d8282e37dec5",
        "label": "Structuring",
        "ctype": "MOTIF",
        "weight": 1.0,
        "tx_ids": [
          "T012"
        ],
        "f": 0.6,
        "l": 0.0,
        "comparison": "STRUCTURING over accounts ACC-BOB (3 tx)",
        "generic": false,
        "text": "Structuring: STRUCTURING over accounts ACC-BOB (3 tx) | weight 1.00, F' 0.600 vs L' 0.000, tx T012"
      }
    ],
    "mitigating": [],
    "insufficient": [
      {
        "name": "amount",
        "label": "Amount",
        "ctype": "SENSOR",
        "weight": 1.0,
        "tx_ids": [
          "T010",
          "T011",
          "T012"
        ],
        "f": 0.0,
        "l": 0.0,
        "comparison": "insufficient history (0 prior events); insufficient history (1 prior events); insufficient history (2 prior events)",
        "generic": false,
        "text": "Amount: insufficient history (0 prior events); insufficient history (1 prior events); insufficient history (2 prior events) | weight 1.00, F' 0.000 vs L' 0.000, tx T010, T011, T012"
      },
      {
        "name": "counterparty",
        "label": "Counterparty",
        "ctype": "SENSOR",
        "weight": 1.0,
        "tx_ids": [
          "T010",
          "T011",
          "T012"
        ],
        "f": 0.0,
        "l": 0.0,
        "comparison": "insufficient history (0 prior events); insufficient history (1 prior events); insufficient history (2 prior events)",
        "generic": false,
        "text": "Counterparty: insufficient history (0 prior events); insufficient history (1 prior events); insufficient history (2 prior events) | weight 1.00, F' 0.000 vs L' 0.000, tx T010, T011, T012"
      },
      {
        "name": "drift",
        "label": "Drift",
        "ctype": "SENSOR",
        "weight": 1.0,
        "tx_ids": [
          "T010",
          "T011",
          "T012"
        ],
        "f": 0.0,
        "l": 0.0,
        "comparison": "insufficient history (0 prior events); insufficient history (1 prior events); insufficient history (2 prior events)",
        "generic": false,
        "text": "Drift: insufficient history (0 prior events); insufficient history (1 prior events); insufficient history (2 prior events) | weight 1.00, F' 0.000 vs L' 0.000, tx T010, T011, T012"
      },
      {
        "name": "geo",
        "label": "Geo",
        "ctype": "SENSOR",
        "weight": 1.0,
        "tx_ids": [
          "T010",
          "T011",
          "T012"
        ],
        "f": 0.0,
        "l": 0.0,
        "comparison": "missing field lat, lon",
        "generic": false,
        "text": "Geo: missing field lat, lon | weight 1.00, F' 0.000 vs L' 0.000, tx T010, T011, T012"
      },
      {
        "name": "timing",
        "label": "Timing",
        "ctype": "SENSOR",
        "weight": 1.0,
        "tx_ids": [
          "T010",
          "T011",
          "T012"
        ],
        "f": 0.0,
        "l": 0.0,
        "comparison": "insufficient history (0 prior events); insufficient history (1 prior events); insufficient history (2 prior events)",
        "generic": false,
        "text": "Timing: insufficient history (0 prior events); insufficient history (1 prior events); insufficient history (2 prior events) | weight 1.00, F' 0.000 vs L' 0.000, tx T010, T011, T012"
      },
      {
        "name": "velocity",
        "label": "Velocity",
        "ctype": "SENSOR",
        "weight": 1.0,
        "tx_ids": [
          "T010",
          "T011",
          "T012"
        ],
        "f": 0.0,
        "l": 0.0,
        "comparison": "insufficient history (0 prior events); insufficient history (1 prior events); insufficient history (2 prior events)",
        "generic": false,
        "text": "Velocity: insufficient history (0 prior events); insufficient history (1 prior events); insufficient history (2 prior events) | weight 1.00, F' 0.000 vs L' 0.000, tx T010, T011, T012"
      }
    ],
    "notes": [
      "T010: missing field lat (sensors geo)",
      "T010: missing field lon (sensors geo)",
      "T011: missing field lat (sensors geo)",
      "T011: missing field lon (sensors geo)",
      "T012: missing field lat (sensors geo)",
      "T012: missing field lon (sensors geo)"
    ],
    "message": null,
    "text": "HIGH | risk 0.800 | Bel 0.600 Pl 1.000 U 0.400 K 0.000 | at risk 29350.00 EUR\nReasons (1):\n  1. Structuring: STRUCTURING over accounts ACC-BOB (3 tx) | weight 1.00, F' 0.600 vs L' 0.000, tx T012\nInsufficient evidence (6):\n  - Amount: insufficient history (0 prior events); insufficient history (1 prior events); insufficient history (2 prior events) | weight 1.00, F' 0.000 vs L' 0.000, tx T010, T011, T012\n  - Counterparty: insufficient history (0 prior events); insufficient history (1 prior events); insufficient history (2 prior events) | weight 1.00, F' 0.000 vs L' 0.000, tx T010, T011, T012\n  - Drift: insufficient history (0 prior events); insufficient history (1 prior events); insufficient history (2 prior events) | weight 1.00, F' 0.000 vs L' 0.000, tx T010, T011, T012\n  - Geo: missing field lat, lon | weight 1.00, F' 0.000 vs L' 0.000, tx T010, T011, T012\n  - Timing: insufficient history (0 prior events); insufficient history (1 prior events); insufficient history (2 prior events) | weight 1.00, F' 0.000 vs L' 0.000, tx T010, T011, T012\n  - Velocity: insufficient history (0 prior events); insufficient history (1 prior events); insufficient history (2 prior events) | weight 1.00, F' 0.000 vs L' 0.000, tx T010, T011, T012\nRecords (6):\n  - T010: missing field lat (sensors geo)\n  - T010: missing field lon (sensors geo)\n  - T011: missing field lat (sensors geo)\n  - T011: missing field lon (sensors geo)\n  - T012: missing field lat (sensors geo)\n  - T012: missing field lon (sensors geo)"
  }
};

/** GET /api/v1/metrics after the sample run (no Evaluation_Monitor configured). */
export const sampleMetrics: Record<string, unknown> = {
  "loop": {
    "processed": 13,
    "withheld_up_front": 0,
    "storage_errors": 0,
    "queue_length": 0,
    "latency_ms": {
      "window": {
        "PERCEIVE": {
          "count": 13,
          "p50": 0.1412999990861863,
          "p95": 0.20489999587880448,
          "p99": 0.20489999587880448,
          "min": 0.11150000500492752,
          "max": 0.20489999587880448
        },
        "REMEMBER": {
          "count": 13,
          "p50": 0.17169999773614109,
          "p95": 0.22519999765791,
          "p99": 0.22519999765791,
          "min": 0.10989999282173812,
          "max": 0.22519999765791
        },
        "REASON": {
          "count": 13,
          "p50": 0.10990000009769574,
          "p95": 0.3253999966545962,
          "p99": 0.3253999966545962,
          "min": 0.08480000542476773,
          "max": 0.3253999966545962
        },
        "ACT": {
          "count": 13,
          "p50": 0.04730000364361331,
          "p95": 0.6520999959320761,
          "p99": 0.6520999959320761,
          "min": 0.029999995604157448,
          "max": 0.6520999959320761
        },
        "END_TO_END": {
          "count": 13,
          "p50": 0.5425999988801777,
          "p95": 1.206699998874683,
          "p99": 1.206699998874683,
          "min": 0.34359999699518085,
          "max": 1.206699998874683
        }
      },
      "since_start": {
        "PERCEIVE": {
          "count": 13,
          "p50": 0.1412999990861863,
          "p95": 0.20489999587880448,
          "p99": 0.20489999587880448,
          "min": 0.11150000500492752,
          "max": 0.20489999587880448
        },
        "REMEMBER": {
          "count": 13,
          "p50": 0.17169999773614109,
          "p95": 0.22519999765791,
          "p99": 0.22519999765791,
          "min": 0.10989999282173812,
          "max": 0.22519999765791
        },
        "REASON": {
          "count": 13,
          "p50": 0.10990000009769574,
          "p95": 0.3253999966545962,
          "p99": 0.3253999966545962,
          "min": 0.08480000542476773,
          "max": 0.3253999966545962
        },
        "ACT": {
          "count": 13,
          "p50": 0.04730000364361331,
          "p95": 0.6520999959320761,
          "p99": 0.6520999959320761,
          "min": 0.029999995604157448,
          "max": 0.6520999959320761
        },
        "END_TO_END": {
          "count": 13,
          "p50": 0.5425999988801777,
          "p95": 1.206699998874683,
          "p99": 1.206699998874683,
          "min": 0.34359999699518085,
          "max": 1.206699998874683
        }
      }
    }
  },
  "stages": {
    "PERCEIVE": {
      "count": 13,
      "mean_ms": 0.14587692343271697,
      "total": 13
    },
    "REMEMBER": {
      "count": 13,
      "mean_ms": 0.17026922936425903,
      "total": 13
    },
    "REASON": {
      "count": 13,
      "mean_ms": 0.14169230864634022,
      "total": 13
    },
    "ACT": {
      "count": 13,
      "mean_ms": 0.1976615379904755,
      "total": 13
    },
    "END_TO_END": {
      "count": 13,
      "mean_ms": 0.6554999994337917,
      "total": 13
    }
  },
  "ingest": {
    "accepted": 13,
    "delivered": 13,
    "malformed": 1,
    "duplicate": 1,
    "out_of_order": 0,
    "late": 0,
    "queue_length": 0,
    "backpressure": false,
    "backpressure_limit": 10000,
    "rate_eps": 0.0
  },
  "latency_breach": {
    "active": false,
    "count": 0,
    "threshold_ms": 50.0,
    "p95_ms": 1.206699998874683
  },
  "evaluation": null,
  "ws": {
    "clients": {},
    "dropped": 0,
    "published": 0,
    "queue_size": 256
  },
  "security": {
    "authentication": false,
    "dashboard_origin": "http://localhost:5173",
    "allow_missing_ws_origin": false,
    "ws_origin_rejected": 0,
    "body_too_large": 0
  }
};
