# Implementation Plan: Helix Fraud Immune System

## Overview

Build the Python 3.10 backend module by module (codec → fusion → sensors → graph → cases → pipeline), then storage/API, the React dashboard, learning loop, and the SOC layer last.

## Tasks

- [x] 1. Scaffold
  - [x] 1.1 Create backend/ with pyproject/requirements.txt (design pins, Python 3.10), package skeleton per design Project Structure, HelixConfig + validate(), SimClock, and pytest/Hypothesis profiles, with unit + Hypothesis property tests for the design properties of this module. _Requirements: 3.12, 6.11, 24.4_

- [x] 2. Event_Codec + Stream_Ingestor
  - [x] 2.1 Implement Event_Codec with unit + Hypothesis property tests for the design properties of this module. _Requirements: 1_
  - [x] 2.2 Implement Stream_Ingestor with unit + Hypothesis property tests for the design properties of this module. _Requirements: 1, 2_

- [x] 3. Fusion + Risk/Severity
  - [x] 3.1 Implement pure Dempster-Shafer Fusion with unit + Hypothesis property tests for the design properties of this module. _Requirements: 6_
  - [x] 3.2 Implement Risk/Severity scoring with unit + Hypothesis property tests for the design properties of this module. _Requirements: 13.1, 13.2, 13.3, 13.7, 13.8, 13.9, 13.10, 13.11, 13.12_

- [x] 4. Profile_Store + Change_Point_Detector
  - [x] 4.1 Implement Profile_Store/Behavioural_DNA with unit + Hypothesis property tests for the design properties of this module. _Requirements: 8_
  - [x] 4.2 Implement BOCPD Change_Point_Detector with unit + Hypothesis property tests for the design properties of this module. _Requirements: 9_

- [x] 5. Sensor_Agents
  - [x] 5.1 Implement the six Sensor_Agents with unit + Hypothesis property tests for the design properties of this module. _Requirements: 5, 7_
  - [x] 5.2 Implement the sensor runner with unit + Hypothesis property tests for the design properties of this module. _Requirements: 5_

- [x] 6. Temporal_Graph + Motif_Detectors
  - [x] 6.1 Implement Temporal_Graph with unit + Hypothesis property tests for the design properties of this module. _Requirements: 10_
  - [x] 6.2 Implement Motif_Detectors with unit + Hypothesis property tests (brute-force oracle) for the design properties of this module. _Requirements: 11_

- [x] 7. Case_Manager, Prioritiser, Evidence_Chain, Explanation_Engine
  - [x] 7.1 Implement Case_Manager + Prioritiser with unit + Hypothesis property tests for the design properties of this module. _Requirements: 12, 13, 14_
  - [x] 7.2 Implement Evidence_Chain + Explanation_Engine with unit + Hypothesis property tests for the design properties of this module. _Requirements: 15, 16, 18_

- [x] 8. Pipeline wiring
  - [x] 8.1 Wire SENSE→ACT pipeline with two-stage fusion, with unit + Hypothesis property tests for the design properties of this module. _Requirements: 1, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16_
  - [x] 8.2 Add end-to-end test on docs/sample_transactions.jsonl (low Minimum_History_Count config): 1 duplicate, 1 rejected, Alice T007 high-tier case, Bob structuring case T010-T012, one mule-chain case T013-T015. _Requirements: 1, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16_

- [x] 9. Synthetic data, AMLworld loader, evaluation
  - [x] 9.1 Implement Synthetic_Generator with unit + Hypothesis property tests for the design properties of this module. _Requirements: 3_
  - [x] 9.2 Implement AMLworld Dataset_Loader (hand-written fixture in tests/fixtures/aml) with unit + Hypothesis property tests for the design properties of this module. _Requirements: 4_
  - [x] 9.3 Implement Evaluation_Monitor plus a ground-truth import-isolation test, with unit + Hypothesis property tests for the design properties of this module. _Requirements: 14.12, 20.12, 24.11_

- [x] 10. Storage + Replay
  - [x] 10.1 Implement DuckDB Storage (batched writer, snapshots, restore) with unit + Hypothesis property tests for the design properties of this module. _Requirements: 17, 24_
  - [x] 10.2 Implement Replay service with unit + Hypothesis property tests for the design properties of this module. _Requirements: 24_

- [x] 11. FastAPI API_Server
  - [x] 11.1 Implement REST endpoints and WebSockets with unit + Hypothesis property tests for the design properties of this module. _Requirements: 23, 25_
  - [x] 11.2 Implement security (loopback bind, CORS, WS origin) and metrics with unit + Hypothesis property tests for the design properties of this module. _Requirements: 25, 27_

- [x] 12. React+Vite+TS Dashboard
  - [x] 12.1 Scaffold dashboard with WS client, store, LiveFeed, CaseQueue, Controls. _Requirements: 22_
  - [x] 12.2 Implement EvidencePanel, LoopPanel, and accessibility. _Requirements: 22_
  - [x] 12.3 Implement Network3D, GeoMap, ReplaySlider. _Requirements: 22_

- [x] 13. Run scripts + README
  - [x] 13.1 Add run scripts and README (quickstart, manual AMLworld Kaggle download, security section). _Requirements: 27.6_

- [x] 14. Learning loop
  - [x] 14.1 Implement Feedback_Learner with unit + Hypothesis property tests for the design properties of this module. _Requirements: 19_
  - [x] 14.2 Implement Attacker_Agent with unit + Hypothesis property tests for the design properties of this module. _Requirements: 20_
  - [x] 14.3 Implement Immune_System with unit + Hypothesis property tests for the design properties of this module. _Requirements: 21_

- [x] 15. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 16. SOC/cyber layer (built last)
  - [x] 16.1 Implement Req 28-29 components with unit + Hypothesis property tests for the design properties of this module. _Requirements: 28, 29_
  - [x] 16.2 Implement Req 30-31 components with unit + Hypothesis property tests for the design properties of this module. _Requirements: 30, 31_
  - [x] 16.3 Implement Req 32-33 components with unit + Hypothesis property tests for the design properties of this module. _Requirements: 32, 33_
  - [x] 16.4 Implement Req 34-35 components with unit + Hypothesis property tests for the design properties of this module. _Requirements: 34, 35_
  - [x] 16.5 Implement Req 36-37 components with unit + Hypothesis property tests for the design properties of this module. _Requirements: 36, 37_

- [x] 18. SOC integration
  - [x] 18.1 Mount all SOC routers (soc_routes, soc_response_routes, soc_intel_routes, soc_ops_routes) in create_app/secure_app, build SOC components (RuleEngine, PlaybookRunner, CanaryRegistry, IndicatorStore, AuditLedger, SocConsole, PurpleTeam, ResilienceMode) in AppState with Storage persistence, add SocConfig fields and a `--soc` CLI flag, with API tests. _Requirements: 28, 29, 30, 31, 32, 33, 34, 35, 36, 37_
  - [x] 18.2 Wire SOC components into the pipeline behind soc.enabled: canary and indicator contributions in stage-1 fusion, canary Case creation and Prioritiser exemption, generator/Profile_Store/Immune exclusion of canaries, attacker canary population and technique tags, PlaybookRunner on Case updates, Audit_Ledger hooks for labels, escalations, weight changes, rule and config changes, with unit + Hypothesis property tests. _Requirements: 18.9, 30.5, 32, 33, 34, 35_

- [x] 19. Hardening
  - [x] 19.1 Fix the Case snowball and per-event O(case size) ACT cost (bounded case merging, incremental Evidence_Chain/kill-chain/persist), keep the default generator config responsive, with unit + Hypothesis property tests and a perf test. _Requirements: 12, 25_
  - [x] 19.2 Restore state on restart from Storage, flush Storage before timeline reads, fix the test_monitor_is_wired_to_the_pipeline wait race, with tests. _Requirements: 24_

- [x] 20. Showcase dashboard
  - [x] 20.1 Add tabbed dashboard shell and the SOC console view. _Requirements: 38_
  - [x] 20.2 Implement the Red-vs-Blue arena view. _Requirements: 38_
  - [x] 20.3 Implement the transaction trace endpoint and view, the live log view and the guided tour. _Requirements: 38_

- [x] 22. Viability: datasets, onboarding, workload
  - [x] 22.1 Implement dataset streaming into the live API_Server and the generic CSV adapter with warm-up and the onboarding script, with unit + Hypothesis property tests for the design properties of this module. _Requirements: 39, 40_
  - [x] 22.2 Implement auto-triage, suggested label, bulk feedback endpoint and workload metrics, with unit + Hypothesis property tests for the design properties of this module. _Requirements: 41_
  - [x] 22.3 Implement the dashboard dataset status panel, suggested label, bulk labelling and keyboard shortcuts, with vitest + axe tests. _Requirements: 39, 41_

- [x] 21. Checkpoint - Ensure all tests pass, extend the Playwright smoke test to the new views, refresh docs/dashboard.png, and write the final README (story, how it works, every scenario, AMLworld evaluation, onboarding, deployment for small and large organisations, limitations)
  - Ensure all tests pass, ask the user if questions arise.

- [x] 23. AMLworld benchmark run
  - [x] 23.1 Run the AMLworld HI-Small evaluation on the downloaded data (data/HI-Small_Trans.csv, data/HI-Small_Patterns.txt) with scripts/aml.ps1, record the real precision, recall, captured fraud fraction and per-pattern detection in the README Evaluation section, and verify live streaming with demo.ps1 -Dataset amlworld. _Requirements: 4, 39_

- [ ] 24. Complete restart restore
  - [ ] 24.1 Persist and restore the Antibody_Pool (antibodies with status, lineage and Self_Set) and Feedback_Labels via the existing antibodies/feedback Storage tables, rebuild the Stream_Ingestor dedup set from stored tx_ids, persist and restore the workload counters, and persist and re-attach Motif instances to restored Cases so a later recompute keeps their stage-2 evidence, with unit + Hypothesis property tests (restart-equivalence: state after restore equals state before shutdown). _Requirements: 19, 21, 24, 41.10_

- [x] 25. README walkthrough
  - [x] 25.1 Add a plain-language walkthrough to README.md: one real transaction traced through every stage with its actual numbers, the inner per-event loop and the outer learning loops, the build journey from scratch to end, and a plain-words glossary. _Requirements: 22, 27.6, 38_

- [x] 26. Showcase frontend: theme and Workflow simulation
  - [x] 26.1 Apply the dark AuthKit-inspired theme and motion system across the dashboard. _Requirements: 42_
  - [x] 26.2 Implement the Workflow tab: lazy-loaded 3D simulation with story and live modes. _Requirements: 43_
  - [x] 26.3 Checkpoint: vitest, build, Playwright smoke test with the Workflow tab, screenshots and README frontend sections. _Requirements: 42, 43_

- [x] 27. AuthKit-style redesign
  - [x] 27.1 Rebrand: AuthKit-style navy theme tokens, starfield, grid frame and spotlight backgrounds, HELIX logo, loading splash, header and navigation, and Home route wiring. _Requirements: 42_
  - [x] 27.2 Build the Home landing page: hero, fanned product cards, six-stage loop, features, live workflow preview and calls to action. _Requirements: 42, 43_
  - [x] 27.3 Declutter the Live dashboard: KPI strip, queue, case workspace with sub-tabs, compact feed and loop, and a controls drawer. _Requirements: 22, 42_
  - [x] 27.4 Polish the showcase views (SOC console, Red vs Blue, Transaction trace, Live log, Guided tour, Workflow chrome) to the new design. _Requirements: 38, 42_

- [x] 28. Pricing tab
  - [x] 28.1 Remove the Tour tab and guided tour, and add a Pricing tab with illustrative organisation plans and a monthly/annual billing toggle. _Requirements: 38, 42_

- [ ] 29. AMLworld improvement
  - [ ] 29.1 Improve HELIX on AMLworld: map or add motif detectors for the unmapped pattern types, tune the alert threshold and budget on a training day and evaluate on a separate test day, re-run the benchmark and update the README results. _Requirements: 4, 11_

- [x] 30. AMLworld benchmark and ROI
  - [x] 30.1 Build the honest head-to-head benchmark in benchmarks/: export HELIX alerts from a warm-up then test replay, add the rule and Isolation Forest baselines, compare at equal alert limits (25, 50, 100 per hour) with ROI metrics, and write the results table, chart and BENCHMARK_NOTES.md. _Requirements: 4, 39, 41_
  - [x] 30.2 Add the XGBoost baseline (time-based split, same features) to the benchmark and re-run the comparison. _Requirements: 4_
  - [x] 30.3 Add a Benchmark section to the frontend showing the measured comparison and ROI numbers from the benchmark results. _Requirements: 38, 42_

- [ ] 31. Live demo liveliness
  - [ ] 31.1 Backend: generator warm-up history at startup, an opt-in simulated investigator that labels top Cases from generator ground truth through the normal label path, genome parameters in GET /attacker, and status fields for both, with tests. _Requirements: 3, 19, 20, 22_
  - [x] 31.2 Frontend: simulated-analyst indicator, explained n/a states in Red vs Blue and the loop panel, Trace recent chips that favour high-risk transactions, and a compact dataset-stream line, with tests. _Requirements: 22, 38_

- [ ] 32. Render deployment
  - [-] 32.1 Deploy to Render: single Docker web service serving the built dashboard and API on one origin, password (HTTP Basic) auth on every route and WebSocket via environment variables, configurable host, port and dashboard origin, render.yaml blueprint, with tests. _Requirements: 27_

- [ ]* 17. Optional: Narrative_Generator
  - [ ]* 17.1 Implement Narrative_Generator with unit + Hypothesis property tests for the design properties of this module. _Requirements: 26_

## Notes

- Tasks marked with `*` are optional.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["2.1", "3.1", "4.1", "6.1"] },
    { "id": 2, "tasks": ["2.2", "3.2", "4.2", "6.2"] },
    { "id": 3, "tasks": ["5.1", "7.1", "9.1", "10.1"] },
    { "id": 4, "tasks": ["5.2", "7.2", "9.2", "10.2"] },
    { "id": 5, "tasks": ["8.1", "9.3"] },
    { "id": 6, "tasks": ["8.2", "11.1", "14.1"] },
    { "id": 7, "tasks": ["11.2", "12.1", "14.2"] },
    { "id": 8, "tasks": ["12.2", "14.3"] },
    { "id": 9, "tasks": ["12.3", "16.1"] },
    { "id": 10, "tasks": ["13.1", "16.2"] },
    { "id": 11, "tasks": ["16.3", "17.1"] },
    { "id": 12, "tasks": ["16.4"] },
    { "id": 13, "tasks": ["16.5"] },
    { "id": 14, "tasks": ["18.1", "19.1", "20.1"] },
    { "id": 15, "tasks": ["18.2", "19.2", "20.2"] },
    { "id": 16, "tasks": ["20.3"] },
    { "id": 17, "tasks": ["22.1", "22.2"] },
    { "id": 18, "tasks": ["22.3"] },
    { "id": 19, "tasks": ["21"] },
    { "id": 20, "tasks": ["23.1"] },
    { "id": 21, "tasks": ["24.1"] },
    { "id": 22, "tasks": ["25.1"] },
    { "id": 23, "tasks": ["26.1", "26.2"] },
    { "id": 24, "tasks": ["27.1", "27.2", "27.3", "27.4"] },
    { "id": 25, "tasks": ["26.3"] },
    { "id": 26, "tasks": ["28.1"] },
    { "id": 27, "tasks": ["30.1"] },
    { "id": 28, "tasks": ["29.1", "30.2"] },
    { "id": 29, "tasks": ["30.3"] },
    { "id": 30, "tasks": ["31.1", "31.2"] }
  ]
}
```
