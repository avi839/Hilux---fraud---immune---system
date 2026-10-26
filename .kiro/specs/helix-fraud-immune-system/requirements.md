# Requirements Document

## Introduction

HELIX is a "living" fraud immune system for the F-01 Financial Fraud and Transaction Risk Detection challenge. Instead of a one-shot batch pipeline (load → preprocess → threshold → classify → show), HELIX runs as a robotics-style closed control loop over a continuous transaction stream:

SENSE → PERCEIVE → REMEMBER → REASON → ACT → LEARN

- SENSE ingests transaction events one at a time from a live stream.
- PERCEIVE runs a swarm of independent specialist sensor agents whose evidence is fused with Dempster-Shafer evidence theory, so missing or incomplete information is carried as explicit uncertainty rather than treated as zero risk.
- REMEMBER keeps an online-updated behavioural DNA profile per account and uses Bayesian Online Change-Point Detection to separate lasting legitimate regime shifts from abrupt suspicious breaks.
- REASON maintains a streaming temporal transaction graph and detects temporal motifs (round-tripping cycles, fan-out/fan-in, structuring, rapid pass-through mule chains) that make collections of individually normal transactions suspicious.
- ACT ranks cases under a hard investigator-capacity alert budget and presents each case with a severity tier, a deterministic evidence chain, a related-accounts subgraph and a time-travel replay timeline.
- LEARN adapts sensor weights from investigator feedback, and co-evolves with an adversarial attacker agent through an Artificial Immune System that spawns new antibody detectors for previously unseen patterns.

HELIX also adopts a SOC / cyber-fusion framing: it treats fraud as a security incident rather than an isolated transaction anomaly. Authentication telemetry is fused with payment evidence, Cases are reconstructed as kill chains and mapped to a technique catalogue, and detection-as-code rules, simulated response playbooks, deception honeytokens, threat-intelligence indicators and a tamper-evident audit ledger support a security-operations workflow.

Target stack: Python 3.10+, FastAPI with asyncio and WebSockets, River for online learning, NetworkX or igraph for the temporal graph, DuckDB for storage, a React + Vite frontend with react-force-graph and deck.gl, and pytest + Hypothesis for property-based testing.

Delivery priority for the prototype:

- MVP = Req 1-9, 13-18, 22-25 and Req 4 (AMLworld benchmark replay).
- Increment 2 = Req 10-12.
- Increment 3 = Req 19-21.
- Increment 4 (SOC/cyber-fusion, implemented last) = Req 28-37.
- Optional = Req 26.

Authentication and authorisation are out of scope for this local demonstration prototype. This is recorded as a security consideration in Requirement 27.

## Glossary

- **HELIX**: The complete fraud immune system described in this document.
- **Transaction_Event**: A single financial transaction record containing at minimum a transaction identifier, timestamp, source account identifier, destination account identifier, amount and currency, and optionally channel, merchant category, latitude, longitude, device identifier and session identifier.
- **Optional_Field**: A Transaction_Event field that may be absent (channel, merchant category, latitude, longitude, device identifier, session identifier).
- **Event_Codec**: The component that parses serialized Transaction_Events (JSON) into internal Transaction_Event objects and formats Transaction_Event objects back into JSON.
- **Auth_Event**: A single authentication or account-security event containing an event identifier, timestamp, account identifier, session identifier and event type (LOGIN_SUCCESS, LOGIN_FAILURE, PASSWORD_RESET, MFA_SUCCESS, MFA_FAILURE or NEW_PAYEE_ADDED), and optionally a device identifier, IP address, latitude, longitude and, for NEW_PAYEE_ADDED, the payee account identifier.
- **Auth_Event_Codec**: The component that parses serialized Auth_Events (JSON) into internal Auth_Event objects and formats Auth_Event objects back into JSON.
- **Stream_Ingestor**: The component that receives Transaction_Events from a stream source and passes them one at a time into the HELIX loop.
- **Stream_Source**: Any producer of Transaction_Events connected to the Stream_Ingestor (Synthetic_Generator, Attacker_Agent, Dataset_Loader replay, or the ingestion API).
- **Synthetic_Generator**: The component that produces realistic synthetic legitimate Transaction_Events for a population of simulated accounts, including legitimate behaviour changes.
- **Dataset_Loader**: The component that reads  AMLworld CSIBMV files (and optional pattern files) and replays the records as Transaction_Events.
- **AMLworld_Pattern**: A laundering attempt listed in an AMLworld pattern file, with its pattern type (FAN-OUT, FAN-IN, CYCLE, GATHER-SCATTER, SCATTER-GATHER, STACK, RANDOM or BIPARTITE) and its transaction rows.
- **Sensor_Agent**: An independent specialist detector that inspects a Transaction_Event in the context of account history and emits a Mass_Function. The built-in Sensor_Agents are Amount_Sensor, Velocity_Sensor, Timing_Sensor, Geo_Sensor, Counterparty_Sensor, Drift_Sensor and Auth_Sensor.
- **Amount_Sensor**: The Sensor_Agent that assesses how unusual a transaction amount is relative to the account's Behavioural_DNA.
- **Velocity_Sensor**: The Sensor_Agent that assesses transaction frequency over sliding time windows.
- **Timing_Sensor**: The Sensor_Agent that assesses time-of-day and day-of-week deviation from the account's usual activity hours.
- **Geo_Sensor**: The Sensor_Agent that assesses physically implausible location changes between consecutive transactions (geo-jump).
- **Counterparty_Sensor**: The Sensor_Agent that assesses the novelty of the destination account relative to the account's known counterparties.
- **Drift_Sensor**: The Sensor_Agent that reports Change_Point_Detector outputs as evidence.
- **Auth_Sensor**: The Sensor_Agent that assesses whether a Transaction_Event follows Suspicious_Auth_Activity on the same account within the Auth_Correlation_Window.
- **Suspicious_Auth_Activity**: Any of the following on one account: at least the configured failed-login count (default 5) of LOGIN_FAILURE Auth_Events within 10 minutes, a PASSWORD_RESET, an MFA_FAILURE, a NEW_PAYEE_ADDED, or a LOGIN_SUCCESS from a device identifier not previously seen for the account.
- **Auth_Correlation_Window**: The configurable time window preceding a Transaction_Event within which Auth_Events are correlated with the Transaction_Event (default 30 minutes).
- **Frame_Of_Discernment**: The set {FRAUD, LEGIT}; the set of hypotheses over which Mass_Functions are defined.
- **Mass_Function**: A Dempster-Shafer basic probability assignment over the subsets {FRAUD}, {LEGIT} and {FRAUD, LEGIT}, with every mass in [0, 1] and the masses summing to 1.
- **Uncertainty_Mass**: The mass assigned to {FRAUD, LEGIT}, representing explicit "unknown".
- **Vacuous_Mass_Function**: The Mass_Function with Uncertainty_Mass equal to 1.
- **Belief**: Bel(FRAUD), equal to the mass on {FRAUD}.
- **Plausibility**: Pl(FRAUD), equal to the mass on {FRAUD} plus the Uncertainty_Mass.
- **Conflict_Coefficient**: The Dempster-Shafer conflict K between two Mass_Functions, equal to the product-sum of masses on disjoint focal sets.
- **Evidence_Fuser**: The component that combines Mass_Functions from Sensor_Agents, Motif_Detectors, Antibodies, Detection_Rules, Indicator matches and Canary touches using Dempster's rule of combination after applying Sensor_Weights.
- **Sensor_Weight**: A reliability discount factor in [0, 1] applied to a Mass_Function before fusion (Shafer discounting).
- **Fused_Assessment**: The Evidence_Fuser output for one Transaction_Event: the fused Mass_Function, Belief, Plausibility, Uncertainty_Mass and Conflict_Coefficient.
- **Behavioural_DNA**: The per-account profile updated online, containing running statistics of amount, inter-arrival time, active hours, locations, channels and the known counterparty set.
- **Profile_Store**: The component that holds and updates Behavioural_DNA for all accounts.
- **Minimum_History_Count**: The configurable number of applied Transaction_Events an account's Behavioural_DNA must contain before history-dependent evidence is emitted (integer from 1 to 100, default 5).
- **Change_Point_Detector**: The per-account Bayesian Online Change-Point Detection component that maintains a run-length posterior over the account's behavioural feature stream.
- **Regime_Shift**: A change point after which the new behaviour persists for at least the configured Regime_Confirmation_Count of subsequent transactions.
- **Abrupt_Break**: A change point whose new behaviour has not yet persisted for the Regime_Confirmation_Count.
- **Regime_Confirmation_Count**: The configurable number of consistent post-change transactions required to classify a change point as a Regime_Shift (default 10).
- **Temporal_Graph**: The streaming directed multigraph whose nodes are accounts and whose edges are timestamped money flows derived from Transaction_Events.
- **Graph_Window**: The configurable time horizon of edges retained in the Temporal_Graph (default 7 days).
- **Motif_Detector**: A component that detects a specific Motif type in the Temporal_Graph and emits a Mass_Function for each participating Transaction_Event.
- **Motif**: A time-constrained subgraph pattern. The built-in Motif types are Round_Trip_Cycle, Fan_Out, Fan_In, Structuring and Mule_Chain.
- **Round_Trip_Cycle**: A directed cycle of length 2 to 6 accounts whose edges are in increasing timestamp order and complete within 72 hours.
- **Fan_Out**: A single account sending to at least 8 distinct destination accounts within 24 hours.
- **Fan_In**: A single account receiving from at least 8 distinct source accounts within 24 hours.
- **Structuring**: At least 3 transactions from the same source account within 48 hours, each with amount within 10% below the configured Reporting_Threshold.
- **Reporting_Threshold**: The configurable regulatory reporting amount (default 10,000 in the transaction currency).
- **Mule_Chain**: A path of at least 3 accounts in which each intermediate account forwards at least 80% of a received amount to the next account within 60 minutes of receiving the amount.
- **Risk_Score**: A value in [0, 1] computed from the Fused_Assessment for a Transaction_Event or Case.
- **Expected_Loss**: The Risk_Score of a Case multiplied by the total amount at risk in the Case.
- **Investigation_Cost**: The configurable estimated cost of investigating one Case.
- **Case_Priority**: Expected_Loss minus Investigation_Cost.
- **Case**: A unit of investigation grouping one or more related flagged Transaction_Events and the accounts involved.
- **Case_Manager**: The component that creates, merges, updates and stores Cases.
- **Severity_Tier**: One of LOW, MEDIUM, HIGH or CRITICAL, assigned to a Case from configurable Risk_Score bands.
- **Alert_Budget**: The configurable maximum number of Cases that may be in the Active_Queue per budget period (default 20 per hour).
- **Active_Queue**: The ordered list of Cases presented to Investigators for action.
- **Detected_Event**: A Transaction_Event or Auth_Event contained in a Case admitted to the Active_Queue.
- **Backlog**: The ordered list of Cases that qualified for alerting but exceeded the Alert_Budget.
- **Prioritiser**: The component that ranks Cases by Case_Priority and enforces the Alert_Budget.
- **Evidence_Chain**: The ordered, structured record of all evidence supporting a Case: Sensor_Agents, Motif_Detectors and Antibodies that contributed, each contributor's Mass_Function and Sensor_Weight, the Fused_Assessment, contributing Transaction_Event identifiers, any Motif involved, and any change point involved.
- **Kill_Chain_Stage**: One of the ordered attack stages RECONNAISSANCE < CREDENTIAL_ACCESS < ACCOUNT_TAKEOVER < PAYEE_MANIPULATION < CASH_OUT < LAYERING, assigned to each Evidence_Chain entry.
- **Kill_Chain_Multiplier**: The configurable factor, at least 1, applied to a Case's Expected_Loss when the Case's most advanced Kill_Chain_Stage is CASH_OUT or LAYERING (default 1.5).
- **HELIX_Technique**: A fraud or cyber attack technique with an identifier of the form HX-Tnnnn (four digits), a name, a domain (FRAUD or CYBER), a description and a Kill_Chain_Stage.
- **HELIX_Technique_Catalogue**: The versioned collection of HELIX_Techniques. The catalogue is inspired by MITRE ATT&CK but is not official MITRE data.
- **Coverage_Matrix**: The per-HELIX_Technique table listing the contributors that detect the technique and the Attacker_Agent detection rate for the technique.
- **Explanation_Engine**: The component that renders human-readable reasons deterministically from an Evidence_Chain using templates.
- **Related_Subgraph**: The subgraph of the Temporal_Graph containing the Case accounts and their neighbours up to a configurable hop depth (default 2) within the Graph_Window.
- **Replay_Timeline**: The time-ordered reconstruction of Transaction_Events, sensor outputs, Fused_Assessments and graph state for a Case.
- **Investigator**: A human user who reviews Cases and records outcomes.
- **Feedback_Label**: An Investigator decision on a Case: CONFIRMED_FRAUD or FALSE_POSITIVE.
- **Feedback_Learner**: The component that adapts Sensor_Weights and sensor thresholds online from Feedback_Labels.
- **Attacker_Agent**: The adversarial red-team component that generates and mutates synthetic fraud patterns and injects them into the stream.
- **Attack_Campaign**: A set of fraudulent Transaction_Events produced by the Attacker_Agent from one pattern genome, with ground-truth labels held outside the detection path.
- **Pattern_Genome**: The parameterised description of an attack pattern (topology, amounts, timing, hop count, account selection) that the Attacker_Agent mutates.
- **Immune_System**: The Artificial Immune System component that uses clonal selection to create, mutate, validate and retire Antibodies.
- **Antibody**: A dynamically created detector, represented as a parameterised feature-space matcher, that emits a Mass_Function when a Transaction_Event or Motif instance matches it.
- **Self_Set**: A rolling sample of recent Transaction_Events not labelled as fraud, used to validate Antibodies against false positives (negative selection).
- **Antibody_Pool**: The bounded collection of active Antibodies (default maximum 200).
- **Ground_Truth_Store**: The store holding Attacker_Agent labels, accessible only to evaluation and the Immune_System miss-detection path.
- **Evaluation_Monitor**: The component that computes detection metrics (precision, recall, alerts per hour, detection latency) against Ground_Truth_Store labels.
- **Detection_Rule**: A versioned, Sigma-style YAML detection rule containing an identifier, title, version, status, HELIX_Technique tags, a selection-and-condition expression over Transaction_Event, Auth_Event and Behavioural_DNA fields, and the Mass_Function emitted on a match.
- **Rule_Engine**: The component that validates, versions, backtests, activates and evaluates Detection_Rules.
- **Response_Playbook**: A declarative, versioned definition of an ordered list of Playbook_Actions with a trigger on Severity_Tier, HELIX_Technique tags, or both.
- **Playbook_Action**: A simulated response action recorded only inside HELIX: HOLD_TRANSACTION, STEP_UP_AUTH, FREEZE_ACCOUNT or NOTIFY.
- **Playbook_Runner**: The component that matches Cases to Response_Playbooks and executes, queues for approval, or dry-runs Playbook_Actions.
- **Playbook_Approval_Threshold**: The configurable Severity_Tier at or above which Playbook_Actions require Investigator approval (default HIGH).
- **Canary_Account**: A seeded decoy account identifier that no legitimate traffic uses (honeytoken).
- **Canary_Payee**: A seeded decoy payee or destination account identifier that no legitimate traffic uses (honeytoken).
- **Canary_Device**: A seeded decoy device identifier that no legitimate traffic uses (honeytoken).
- **Canary**: Any Canary_Account, Canary_Payee or Canary_Device.
- **Indicator**: A threat-intelligence record for an account identifier, device identifier, IP address or merchant identifier, with type, confidence (integer 0–100), source, first_seen, last_seen, expiry and TLP_Marking.
- **Indicator_Store**: The component that holds, matches, extracts, imports and exports Indicators.
- **TLP_Marking**: A Traffic Light Protocol sharing label: TLP:CLEAR, TLP:GREEN, TLP:AMBER or TLP:RED.
- **Audit_Ledger**: The append-only, SHA-256 hash-chained record of security-relevant HELIX actions. The Audit_Ledger is tamper-evident, not tamper-proof.
- **SLA_Timer**: The per-Case timers measuring time-to-acknowledge (creation to IN_REVIEW) and time-to-close (creation to CLOSED) against targets for the Case's Severity_Tier.
- **SOC_Console**: The Dashboard view for security-operations oversight: SLA_Timers, analyst workload, shift handoff notes, Case age, mean-time-to-detect and mean-time-to-respond.
- **Purple_Team_Exercise**: A run of a defined set of Attack_Campaigns against an isolated copy of the HELIX detection state.
- **Exercise_Scorecard**: The stored result of a Purple_Team_Exercise: per-HELIX_Technique coverage, detection rate, median detection latency, alert volume and false-positive count.
- **Resilience_Mode**: The optional mode, disabled by default, that injects sensor failures, delayed events and traffic bursts to test degraded operation.
- **Storage**: The DuckDB-backed persistence layer.
- **API_Server**: The FastAPI application exposing REST and WebSocket endpoints.
- **Dashboard**: The React + Vite web frontend.
- **Narrative_Generator**: The optional component that produces a free-text Case narrative using an LLM from an Evidence_Chain.
- **HELIX_Config**: The configuration file containing all tunable thresholds, windows, budgets and seeds.
- **Dataset_Feeder**: The API_Server background task that streams a dataset (AMLworld through the Dataset_Loader, or a company CSV through the CSV_Adapter) into the Stream_Ingestor of the running HELIX backend, in the same way the Synthetic_Generator feeder does.
- **Demo_Script**: The `scripts\demo.ps1` run script that starts the API_Server and the Dashboard.
- **Column_Mapping**: A versioned YAML or JSON document that maps the columns of a company CSV export to Transaction_Event fields, with the timestamp format and time zone, amount and currency handling, optional Optional_Field columns and an optional label column.
- **CSV_Adapter**: The component that validates Column_Mappings and parses or formats CSV rows into or from Transaction_Events under a Column_Mapping.
- **Warm_Up_Mode**: The Dataset_Feeder mode that replays historical rows at maximum speed to build Behavioural_DNA, Change_Point_Detector state and the Temporal_Graph while Case creation is suppressed, before switching to live processing.
- **Onboard_Validator**: The command-line check, run by `scripts\onboard.ps1`, that applies a Column_Mapping to the first rows of a CSV file and prints a parse report without starting the HELIX loop.
- **Auto_Triage**: The optional component, disabled by default, that closes idle LOW-tier Cases with the Closure_Reason AUTO_CLEARED.
- **Closure_Reason**: The reason a Case is CLOSED: LABELLED (a Feedback_Label was stored), MERGED (the Case was merged into a survivor) or AUTO_CLEARED (closed by the Auto_Triage). AUTO_CLEARED is not a Feedback_Label.
- **Label_Suggester**: The pure component that derives a Suggested_Label from a Case's fused Belief and Plausibility.
- **Suggested_Label**: A non-binding recommendation shown to the Investigator for a Case: CONFIRMED_FRAUD, FALSE_POSITIVE or NONE, with a confidence in [0, 1] and a reason text. A Suggested_Label is never stored as a Feedback_Label.
- **Alert_Threshold**: The configured `risk.alert_threshold` (default 0.6); a Risk_Score at or above the Alert_Threshold makes a Transaction_Event suspicious and eligible for a Case.
- **Theme**: The Dashboard visual language of Requirement 42: Design_Tokens, Glass_Cards, glow backgrounds, typography and the motion catalogue.
- **Design_Token**: A CSS custom property with the `--hx-` prefix, defined on `:root`, that holds one colour, radius, font, easing or duration value of the Theme.
- **Glass_Card**: A Dashboard panel container drawn with the `--hx-surface` background, a 1 px `--hx-border` border, `--hx-radius` corners and a backdrop blur.
- **Workflow_View**: The Dashboard showcase tab labelled "Workflow" that hosts the Workflow_Scene, its controls, the Caption_Panel and the Text_View.
- **Workflow_Scene**: The lazily loaded 3D scene of the Workflow_View that animates transactions through the HELIX loop.
- **Story_Mode**: The default Workflow_View mode that plays the Story_Script without an API_Server connection.
- **Story_Script**: The deterministic, ordered timeline of steps that tells the sample scenario (Alice's takeover, Bob's structuring and the mule chain) in the Workflow_View.
- **Live_Mode**: The Workflow_View mode that drives the Workflow_Scene from live WebSocket frames.
- **Live_Adapter**: The pure component that maps WebSocket frames and store updates to Sim_Events for the Sim_Model.
- **Sim_Model**: The pure simulation stepper that advances Sim_Particles, Case_Cards and Learning_Pulses over time without rendering.
- **Sim_Event**: An input to the Sim_Model: spawn transaction, open Case, label Case, attack injection, antibody change, canary touch or playbook hold.
- **Sim_Particle**: One transaction drawn in the Workflow_Scene as it moves from its Sender figure through the Stage_Rings.
- **Stage_Ring**: One of the six labelled rings of the Workflow_Scene core, in the order SENSE, PERCEIVE, REMEMBER, REASON, ACT, LEARN.
- **Case_Card**: The Workflow_Scene object that represents a Case and carries its Case identifier and Severity_Tier as text.
- **Learning_Pulse**: The Workflow_Scene object that travels from the Investigator figure back to the core after a Feedback_Label.
- **Particle_Cap**: The maximum number of active Sim_Particles in the Workflow_Scene (400).
- **Caption_Panel**: The Workflow_View side panel that names the current stage and describes it in plain words.
- **Text_View**: The Workflow_View alternative that presents the same story as an ordered list of steps.

## Requirements

### Requirement 1: Continuous Stream Ingestion (SENSE)

**User Story:** As a fraud operations lead, I want transactions analysed as they arrive, so that suspicious activity is surfaced while it is still happening rather than after a batch run.

#### Acceptance Criteria

1. WHEN the Event_Codec successfully parses a non-duplicate Transaction_Event received from a Stream_Source, THE Stream_Ingestor SHALL pass the Transaction_Event into the HELIX loop exactly once, as a single unbatched event.
2. THE Stream_Ingestor SHALL deliver the queued Transaction_Events of each source account to the HELIX loop in non-decreasing timestamp order, with ties delivered in arrival order.
3. THE Stream_Ingestor SHALL admit at most one Transaction_Event of each source account into the HELIX loop at a time.
4. IF a Transaction_Event has a timestamp earlier than the latest processed timestamp for the same source account by more than the configured lateness tolerance (default 5 minutes, configurable from 0 seconds to 24 hours), THEN THE Stream_Ingestor SHALL mark the Transaction_Event as late, pass the Transaction_Event into the HELIX loop, and record the late flag and the lateness in seconds in the Evidence_Chain.
5. IF a Transaction_Event has a timestamp earlier than the latest processed timestamp for the same source account by no more than the configured lateness tolerance, THEN THE Stream_Ingestor SHALL pass the Transaction_Event into the HELIX loop and increment the out-of-order counter by 1.
6. THE Stream_Ingestor SHALL retain the transaction identifiers of accepted Transaction_Events for a duplicate-retention horizon of at least the Graph_Window.
7. IF a Transaction_Event has a transaction identifier equal to that of a Transaction_Event accepted within the duplicate-retention horizon, THEN THE Stream_Ingestor SHALL discard the duplicate before the HELIX loop and before publication to the live event channel, and increment the duplicate counter by 1.
8. WHILE the inbound queue length exceeds the configured backpressure limit (default 10,000 events, configurable from 100 to 1,000,000 events), THE Stream_Ingestor SHALL signal backpressure to connected Stream_Sources and retain every queued Transaction_Event.
9. WHILE backpressure is signalled, WHEN the inbound queue length falls to 50% of the configured backpressure limit or below, THE Stream_Ingestor SHALL clear the backpressure signal.
10. IF the Event_Codec returns an error for a received document, including malformed JSON, a missing required field or an amount less than or equal to 0, THEN THE Stream_Ingestor SHALL reject the document, increment the malformed counter by 1, and continue ingesting subsequent documents.
11. WHEN a Transaction_Event completes the ACT stage, THE HELIX SHALL publish the Transaction_Event and its Fused_Assessment to the live event channel of the API_Server.
12. WHILE Transaction_Events arrive at 100 events per second, THE HELIX SHALL publish at least 95% of the Transaction_Events to the live event channel within 1 second of receipt by the Stream_Ingestor.
13. WHILE no client is connected to the live event channel, THE HELIX SHALL continue processing Transaction_Events through every stage of the HELIX loop.

### Requirement 2: Transaction Event Parsing and Formatting

**User Story:** As a developer, I want a strict, well-defined transaction event format, so that every stream source feeds HELIX consistently.

#### Acceptance Criteria

1. WHEN a JSON document conforming to the Transaction_Event schema is provided, THE Event_Codec SHALL parse the JSON document into a Transaction_Event object.
2. THE Event_Codec SHALL validate each JSON document against the Transaction_Event schema, in which every identifier field is a string of 1 to 64 characters, the timestamp is an ISO 8601 date-time with an explicit UTC offset or Z, the amount is a decimal from 0.01 to 999,999,999.99 with at most 2 fractional digits, the currency is 3 uppercase ASCII letters, the latitude is in [-90, 90] and the longitude is in [-180, 180].
3. IF a JSON document violates the Transaction_Event schema, THEN THE Event_Codec SHALL return an error listing every offending field together with the schema rule that the field violated, and produce no Transaction_Event object.
4. WHEN a JSON document omits an Optional_Field or sets an Optional_Field to null, THE Event_Codec SHALL represent the Optional_Field as explicitly missing in the Transaction_Event object, distinct from an empty string, the number 0 and a coordinate value of 0.0.
5. THE Event_Codec SHALL format Transaction_Event objects into JSON documents conforming to the Transaction_Event schema, with amounts written with exactly 2 fractional digits, timestamps written with an explicit UTC offset, and missing Optional_Fields omitted.
6. FOR ALL valid Transaction_Event objects, including objects with any combination of missing Optional_Fields, formatting then parsing SHALL produce a Transaction_Event object equal to the original (round-trip property).
7. THE Event_Codec SHALL represent amounts as exact fixed-point decimal values with 2 fractional digits, so that an input amount of 5 is represented as 5.00 and an input amount of 0.10 is represented exactly.
8. WHEN the Event_Codec rejects a JSON document, THE Stream_Ingestor SHALL write the raw document, the receipt time and the offending field names to the rejected-events log and continue ingesting subsequent documents.
9. WHEN a JSON document contains fields not defined in the Transaction_Event schema, THE Event_Codec SHALL ignore the undefined fields and parse the remaining fields without returning an error for the undefined fields.

### Requirement 3: Synthetic Legitimate Traffic Generator

**User Story:** As a demo presenter, I want realistic synthetic legitimate traffic, so that HELIX can be demonstrated live without external data.

#### Acceptance Criteria

1. THE Synthetic_Generator SHALL simulate a configurable number of accounts from 1 to 100,000 (default 1,000), each with its own amount distribution, active hours, home location, channel mix and a counterparty set of 1 to 50 accounts.
2. WHILE no legitimate behaviour change is in effect for an account, THE Synthetic_Generator SHALL generate at least 95% of that account's Transaction_Events inside the account's active hours, within 50 km of the account's home location, and to destination accounts in the account's counterparty set.
3. WHEN started with a seed from 0 to 4,294,967,295, THE Synthetic_Generator SHALL produce, using a simulated clock, an identical Transaction_Event sequence for every run with the same seed and HELIX_Config.
4. WHEN started without a seed, THE Synthetic_Generator SHALL use the default seed defined in HELIX_Config.
5. THE Synthetic_Generator SHALL emit Transaction_Events at a configurable rate from 1 to 1,000 events per second (default 50), with the number of Transaction_Events emitted in every 10-second window that starts after a 5-second warm-up within ±10% of 10 times the configured rate.
6. THE Synthetic_Generator SHALL assign exactly one legitimate behaviour change (relocation, salary increase, new regular payee or travel) to exactly round(change fraction × account count) accounts, where the change fraction is configurable (default 0.05) and halves are rounded up.
7. WHEN the configured duration of a travel behaviour change elapses, THE Synthetic_Generator SHALL revert the account to its pre-travel behaviour.
8. THE Synthetic_Generator SHALL record each legitimate behaviour change in the Ground_Truth_Store, labelled legitimate, before emitting the first Transaction_Event affected by the change.
9. THE Synthetic_Generator SHALL omit each Optional_Field independently from a configurable fraction of Transaction_Events (default 0.10), with the observed omission fraction of each Optional_Field within ±2 percentage points of the configured fraction over 10,000 Transaction_Events.
10. WHEN the Synthetic_Generator omits an Optional_Field, THE Synthetic_Generator SHALL leave the Optional_Field absent from the Transaction_Event rather than filling the Optional_Field with a placeholder value.
11. FOR ALL Transaction_Events produced by the Synthetic_Generator, the Event_Codec SHALL parse the formatted Transaction_Event without error, and the source account identifier SHALL differ from the destination account identifier.
12. IF any Synthetic_Generator setting in HELIX_Config is outside its permitted range, THEN THE Synthetic_Generator SHALL refuse to start and report an error naming every out-of-range setting.

### Requirement 4: AMLworld Benchmark Dataset Loader

**User Story:** As an evaluator, I want to replay a public benchmark dataset, so that HELIX can be assessed on data it did not generate.

#### Acceptance Criteria

1. WHEN an IBM AMLworld transactions CSV file path is provided, THE Dataset_Loader SHALL parse each CSV row into a Transaction_Event by mapping the Timestamp column to the timestamp, the From Bank and first Account columns to the source account identifier, the To Bank and second Account columns to the destination account identifier, the Amount Paid column to the amount, the Payment Currency column to the currency, and the Payment Format column to the channel, with all other Optional_Fields missing.
2. THE Dataset_Loader SHALL assign each parsed Transaction_Event a transaction identifier derived deterministically from the CSV row number and unique within the file.
3. THE Dataset_Loader SHALL emit parsed Transaction_Events in non-decreasing timestamp order, with ties emitted in CSV row order.
4. THE Dataset_Loader SHALL schedule emissions at a configurable speed multiplier from 1 to 1,000,000 relative to recorded time (default 60) and emit each Transaction_Event within 100 ms of the Transaction_Event's scheduled emission time.
5. THE Dataset_Loader SHALL store each row's laundering label in the Ground_Truth_Store, keyed by transaction identifier, before emitting the corresponding Transaction_Event.
6. THE Dataset_Loader SHALL exclude the laundering label from every emitted Transaction_Event.
7. IF a CSV row cannot be parsed, THEN THE Dataset_Loader SHALL log the row number and the parse failure reason, skip the row, and continue with the next row.
8. WHEN the Dataset_Loader finishes reading a CSV file, THE Dataset_Loader SHALL report the number of parsed rows and the number of skipped rows.
9. THE Dataset_Loader SHALL format Transaction_Events back into AMLworld CSV rows with columns in the AMLworld header order.
10. FOR ALL CSV rows parsed by the Dataset_Loader, parsing then formatting then parsing SHALL produce a Transaction_Event equal to the Transaction_Event from the first parse (round-trip property).
11. IF the CSV file is missing, is unreadable or lacks a required column, THEN THE Dataset_Loader SHALL return an error stating the file path and the reason, and produce no Transaction_Events and no Ground_Truth_Store labels.
12. IF the configured speed multiplier is neither "max" nor a number in the range 1 to 1,000,000, THEN THE Dataset_Loader SHALL refuse to start and report an error naming the speed multiplier setting.
13. WHEN a Timestamp value carries no UTC offset, THE Dataset_Loader SHALL interpret the Timestamp value in the configured dataset timezone (default UTC) and convert the Timestamp value to UTC.
14. THE Dataset_Loader SHALL accept Timestamp values in the form "YYYY/MM/DD HH:MM" and in ISO 8601 date-time form.
15. THE Dataset_Loader SHALL ignore the Amount Received and Receiving Currency columns when parsing, so that neither value is carried on the emitted Transaction_Event or used by any Sensor_Agent, Motif_Detector or Antibody.
16. WHERE a replay scope is configured with a start timestamp, an end timestamp, a maximum row count, or any combination of the three (default: no timestamp window and no row limit), THE Dataset_Loader SHALL emit, and store Ground_Truth_Store labels for, only the rows whose timestamps are at or after the start timestamp and before the end timestamp, up to the maximum row count in emission order.
17. WHEN the speed multiplier is set to "max", THE Dataset_Loader SHALL emit each Transaction_Event as soon as the Stream_Ingestor accepts the previous Transaction_Event instead of scheduling emissions relative to recorded time, while pausing whenever backpressure is signalled, passing Transaction_Events one at a time through every stage of the HELIX loop, and advancing stream time to each Transaction_Event's recorded timestamp.
18. WHEN an AMLworld patterns file path is provided, THE Dataset_Loader SHALL parse each AMLworld_Pattern and store the pattern type and the transaction identifiers of the pattern's member transactions, matched to loaded CSV rows by row content, in the Ground_Truth_Store.
19. IF a pattern row in an AMLworld patterns file matches no loaded CSV row, THEN THE Dataset_Loader SHALL log the pattern row, increment the unmatched-pattern-row counter by 1, report the counter value with the parsed and skipped row counts, and continue parsing the patterns file.
20. WHEN a Dataset_Loader replay finishes, THE Evaluation_Monitor SHALL report precision, recall, Active_Queue admissions per budget period, and the captured fraud fraction as defined in Requirement 14.12, computed against the Ground_Truth_Store labels written by the Dataset_Loader.
21. WHEN a Dataset_Loader replay with a loaded AMLworld patterns file finishes, THE Evaluation_Monitor SHALL report, for each AMLworld_Pattern type, the fraction of AMLworld_Patterns of that type with at least one member transaction that is a Detected_Event, mapping CYCLE to Round_Trip_Cycle, FAN-OUT to Fan_Out and FAN-IN to Fan_In, and reporting GATHER-SCATTER, SCATTER-GATHER, STACK, RANDOM and BIPARTITE as unmapped.
22. WHEN the CSV header row is UTF-8 encoded, with or without a byte-order mark, and contains the column name Account twice, THE Dataset_Loader SHALL map columns by position, treating the first Account column as the source account and the second Account column as the destination account.

### Requirement 5: Specialist Sensor Swarm (PERCEIVE)

**User Story:** As a fraud analyst, I want several independent specialists to look at each transaction, so that different kinds of anomaly are detected and reported separately.

#### Acceptance Criteria

1. WHEN a Transaction_Event enters the PERCEIVE stage, THE HELIX SHALL invoke every enabled Sensor_Agent on the Transaction_Event exactly once.
2. THE HELIX SHALL produce, for each Sensor_Agent, a Mass_Function that is identical regardless of the order in which the Sensor_Agents are invoked and regardless of failures of other Sensor_Agents.
3. WHEN invoked, THE Amount_Sensor SHALL emit a Mass_Function whose mass on {FRAUD} is non-decreasing in |amount − mean| / standard deviation, using the amount mean and standard deviation from the account's Behavioural_DNA.
4. WHEN invoked, THE Velocity_Sensor SHALL count the source account's Transaction_Events over 1-minute, 1-hour and 24-hour sliding windows that include the current Transaction_Event, and emit a Mass_Function whose mass on {FRAUD} is non-decreasing in each window count while the other two window counts are held fixed.
5. WHEN invoked, THE Timing_Sensor SHALL emit a Mass_Function whose mass on {FRAUD} is non-decreasing as the relative frequency of the transaction's hour of day in the account's Behavioural_DNA decreases with the weekday frequency held fixed, and as the relative frequency of the transaction's weekday decreases with the hour-of-day frequency held fixed.
6. WHEN invoked with latitude and longitude present on the current Transaction_Event and a prior Transaction_Event with latitude and longitude recorded for the source account, THE Geo_Sensor SHALL compute the implied travel speed as the great-circle distance divided by the absolute time difference to the most recent such prior Transaction_Event.
7. WHEN the Geo_Sensor computes an implied travel speed, THE Geo_Sensor SHALL emit a Mass_Function with mass on {FRAUD} equal to 0 when the implied travel speed is at or below the configured plausible speed (default 900 km/h), and with mass on {FRAUD} non-decreasing in the implied travel speed above the configured plausible speed.
8. IF the time difference to the prior located Transaction_Event is 0 and the great-circle distance is greater than 0, THEN THE Geo_Sensor SHALL treat the implied travel speed as exceeding every finite speed.
9. IF the current Transaction_Event lacks latitude or longitude, or the source account has no prior Transaction_Event with latitude and longitude recorded, THEN THE Geo_Sensor SHALL emit the Vacuous_Mass_Function.
10. WHEN invoked, THE Counterparty_Sensor SHALL emit a Mass_Function with strictly higher mass on {FRAUD} for a destination account absent from the account's known counterparty set than for a destination account present in the known counterparty set, for otherwise identical Transaction_Events and Behavioural_DNA.
11. WHEN invoked, THE Drift_Sensor SHALL emit the Mass_Function defined in Requirement 9 for the account's Change_Point_Detector classification, which is the Vacuous_Mass_Function when the account has no current candidate change point.
12. THE HELIX SHALL record, for each Sensor_Agent invocation, the Transaction_Event identifier, the Sensor_Agent name, the outcome (SUCCESS, ERROR, INVALID_OUTPUT or TIMEOUT), the resulting Mass_Function and the input features used.
13. THE HELIX SHALL accept a Sensor_Agent output as a valid Mass_Function only when every mass is in [0, 1] and the masses sum to 1 within a tolerance of 1e-9.
14. IF a Sensor_Agent raises an error, returns an invalid Mass_Function, or exceeds the configured per-event time budget (default 10 ms, configurable from 1 ms to 1,000 ms), THEN THE HELIX SHALL substitute the Vacuous_Mass_Function for that Sensor_Agent, record the failure type in the Evidence_Chain, and pass the Mass_Functions of the other Sensor_Agents to the Evidence_Fuser unchanged.
15. WHILE the source account's Behavioural_DNA contains fewer than the Minimum_History_Count of applied Transaction_Events, THE Amount_Sensor, Velocity_Sensor, Timing_Sensor and Counterparty_Sensor SHALL each emit the Vacuous_Mass_Function.

### Requirement 6: Dempster-Shafer Evidence Fusion

**User Story:** As a fraud analyst, I want sensor evidence combined in a way that keeps track of what is unknown, so that I can tell confident alerts from alerts built on thin evidence.

#### Acceptance Criteria

1. THE Evidence_Fuser SHALL produce Mass_Functions whose masses are each in [0, 1] and sum to 1 within a tolerance of 1e-9.
2. WHEN Mass_Functions are received for a Transaction_Event, THE Evidence_Fuser SHALL discount each Mass_Function m by the contributor's Sensor_Weight w as m'({FRAUD}) = w·m({FRAUD}), m'({LEGIT}) = w·m({LEGIT}) and m'({FRAUD, LEGIT}) = 1 − w + w·m({FRAUD, LEGIT}), and combine the discounted Mass_Functions using Dempster's rule of combination.
3. FOR ALL sets of Mass_Functions, including sets whose combination is high-conflict, THE Evidence_Fuser SHALL produce Fused_Assessments equal within 1e-9 for every permutation of the combination order (confluence property).
4. FOR ALL Mass_Functions m, THE Evidence_Fuser SHALL return a Mass_Function equal to m within 1e-9, with a Conflict_Coefficient of 0, when m is combined with the Vacuous_Mass_Function (identity property).
5. THE Evidence_Fuser SHALL report Belief, Plausibility, Uncertainty_Mass and Conflict_Coefficient in every Fused_Assessment, each in [0, 1], with Belief less than or equal to Plausibility and Plausibility minus Belief equal to Uncertainty_Mass within 1e-9.
6. THE Evidence_Fuser SHALL compute the Conflict_Coefficient of a Fused_Assessment as the mass on the empty set of the unnormalised conjunctive combination of all discounted Mass_Functions for the Transaction_Event.
7. IF the Conflict_Coefficient equals 1 or exceeds the configured high-conflict threshold (default 0.95, configurable in the range (0, 1]), THEN THE Evidence_Fuser SHALL combine the discounted Mass_Functions using Yager's rule, assigning the conflicting mass to Uncertainty_Mass, report the unreduced Conflict_Coefficient, and mark the Fused_Assessment as high-conflict.
8. WHEN a contributor's Sensor_Weight is 0, THE Evidence_Fuser SHALL produce a Fused_Assessment equal within 1e-9 to the Fused_Assessment computed without that contributor.
9. IF no Mass_Functions are received for a Transaction_Event, or every received contributor has a Sensor_Weight of 0, THEN THE Evidence_Fuser SHALL produce a Fused_Assessment equal to the Vacuous_Mass_Function with Belief 0, Plausibility 1, Uncertainty_Mass 1 and Conflict_Coefficient 0, representing unknown risk rather than zero risk.
10. IF a contributor supplies an invalid Mass_Function or a Sensor_Weight outside [0, 1], THEN THE Evidence_Fuser SHALL exclude that contributor from the combination and report an error identifying the contributor and the Transaction_Event.
11. IF the configured high-conflict threshold is outside the range (0, 1], THEN THE Evidence_Fuser SHALL refuse to start and report an error naming the high-conflict threshold setting.

### Requirement 7: Incomplete Information Handling

**User Story:** As a fraud analyst, I want missing data shown as uncertainty, so that a transaction with missing fields is neither silently cleared nor falsely accused.

#### Acceptance Criteria

1. IF any input feature required by a Sensor_Agent is missing from the Transaction_Event or from the account's Behavioural_DNA, THEN THE Sensor_Agent SHALL emit the Vacuous_Mass_Function.
2. THE HELIX SHALL treat a Transaction_Event field as a missing Sensor_Agent input feature when the field is absent, null or an empty string, and SHALL treat latitude and longitude as missing together when either is missing.
3. THE HELIX SHALL treat a Behavioural_DNA feature as a missing Sensor_Agent input feature when the Behavioural_DNA holds no recorded value for that feature.
4. WHILE the source account's Behavioural_DNA contains fewer than the Minimum_History_Count of applied Transaction_Events, THE Amount_Sensor, Timing_Sensor, Geo_Sensor and Counterparty_Sensor SHALL each emit a Mass_Function with Uncertainty_Mass of at least 0.5, which the Amount_Sensor, Timing_Sensor and Counterparty_Sensor satisfy by emitting the Vacuous_Mass_Function required by Requirement 5.15.
5. FOR ALL Transaction_Events and identical Profile_Store, Change_Point_Detector and Temporal_Graph state, removing any subset of Optional_Fields SHALL produce a Fused_Assessment with Uncertainty_Mass greater than or equal to the Uncertainty_Mass of the Fused_Assessment for the complete Transaction_Event within 1e-9, wherever neither Fused_Assessment is marked high-conflict (metamorphic property).
6. WHEN a Transaction_Event with one or more missing fields is received, THE HELIX SHALL process the Transaction_Event through every stage of the HELIX loop.
7. WHEN a missing-field or insufficient-history condition determines a Sensor_Agent's Mass_Function, THE HELIX SHALL record in the Evidence_Chain the Sensor_Agent name and the reason, stated as the missing field names or as insufficient history with the count of prior applied Transaction_Events.
8. WHEN a Case is displayed, THE Dashboard SHALL show the Uncertainty_Mass of the Case to 2 decimal places and list each missing-field or insufficient-history condition that contributed to the Uncertainty_Mass together with the Sensor_Agent name.
9. WHEN a Case is displayed and no missing-field or insufficient-history condition contributed to the Uncertainty_Mass, THE Dashboard SHALL show a "none contributed" indication.

### Requirement 8: Behavioural DNA Profiles (REMEMBER)

**User Story:** As a fraud analyst, I want each account's normal behaviour learned continuously, so that alerts are judged against that account's own habits rather than a global rule.

#### Acceptance Criteria

1. WHEN a Transaction_Event completes the PERCEIVE stage, THE Profile_Store SHALL update the source account's Behavioural_DNA online with the Transaction_Event's amount, inter-arrival time, hour of activity, location, channel and counterparty.
2. THE Profile_Store SHALL perform Behavioural_DNA updates with a median update time at 100,000 prior applied Transaction_Events of at most 2 times the median update time at 100 prior applied Transaction_Events.
3. WHEN a Transaction_Event with a missing Optional_Field is applied, THE Profile_Store SHALL leave the Behavioural_DNA statistics derived from that Optional_Field unchanged and update the remaining statistics.
4. THE Profile_Store SHALL compute Sensor_Agent inputs from the Behavioural_DNA state that existed before the current Transaction_Event was applied.
5. FOR ALL sequences of 1 to 10,000 Transaction_Events, the decay-weighted running mean and variance of amount and of inter-arrival time in the Behavioural_DNA SHALL equal the batch weighted mean and variance computed with the same decay weights within a relative tolerance of 1e-6, or an absolute tolerance of 1e-9 when the batch value is 0 (model-based property).
6. THE Profile_Store SHALL weight each prior contribution to Behavioural_DNA statistics by 0.5^(Δt / half-life), where Δt is the elapsed time between Transaction_Event timestamps and the half-life is configurable from 1 to 365 days (default 30 days).
7. WHEN a late Transaction_Event, or any Transaction_Event with a timestamp earlier than the latest applied timestamp for the account, is applied, THE Profile_Store SHALL use Δt = 0 for that Transaction_Event.
8. IF the configured half-life is outside the range 1 to 365 days, THEN THE Profile_Store SHALL refuse to start and report an error naming the half-life setting.
9. WHEN a Regime_Shift is confirmed for an account, THE Profile_Store SHALL, before applying the account's next Transaction_Event, rebase the account's amount, inter-arrival time, active-hours, location and channel statistics on the Transaction_Events since the change point and retain the account's known counterparty set.
10. WHILE a Case for an account is open with Severity_Tier HIGH or CRITICAL, THE Profile_Store SHALL withhold the Transaction_Events of that Case from the account's Behavioural_DNA updates.
11. WHEN a Case first reaches Severity_Tier HIGH or CRITICAL, THE Profile_Store SHALL remove the already-applied contributions of the Case's Transaction_Events from the account's Behavioural_DNA, producing a Behavioural_DNA equal within 1e-6 to the Behavioural_DNA in which those Transaction_Events were never applied.
12. WHEN a FALSE_POSITIVE Feedback_Label is recorded for a Case, THE Profile_Store SHALL apply the Case's withheld Transaction_Events to the Behavioural_DNA in timestamp order.
13. WHEN a CONFIRMED_FRAUD Feedback_Label is recorded for a Case, THE Profile_Store SHALL permanently exclude the Case's withheld Transaction_Events from the Behavioural_DNA.
14. WHEN a Transaction_Event arrives for an account with no Behavioural_DNA, THE Profile_Store SHALL provide an empty Behavioural_DNA, with a Transaction_Event count of 0 and an empty known counterparty set, as Sensor_Agent input.

### Requirement 9: Behavioural Change-Point Detection

**User Story:** As a fraud analyst, I want genuine lifestyle changes distinguished from sudden suspicious breaks, so that legitimate customers who change behaviour are not repeatedly flagged.

#### Acceptance Criteria

1. WHEN a Transaction_Event is applied to an account, THE Change_Point_Detector SHALL update the account's run-length posterior using Bayesian Online Change-Point Detection over at least the amount and inter-arrival time features.
2. THE Change_Point_Detector SHALL maintain a run-length posterior whose probabilities are each in [0, 1] and sum to 1 within a tolerance of 1e-9 after every update.
3. WHEN the posterior probability of a change point at the current Transaction_Event exceeds the configured change-point threshold (default 0.5, configurable in the range [0, 1]), THE Change_Point_Detector SHALL record a candidate change point with the account identifier, Transaction_Event identifier, timestamp and posterior probability, and classify the candidate as an Abrupt_Break.
4. WHEN the Regime_Confirmation_Count of consecutive Transaction_Events after a candidate change point do not trigger a new candidate, THE Change_Point_Detector SHALL reclassify the candidate change point as a Regime_Shift.
5. WHILE an account has at least the Minimum_History_Count of applied Transaction_Events and an unconfirmed Abrupt_Break as the current candidate change point, THE Drift_Sensor SHALL emit a Mass_Function with mass on {FRAUD} of at least the configured abrupt-break mass (default 0.4), mass on {LEGIT} of 0, and the remaining mass as Uncertainty_Mass.
6. WHILE an account has at least the Minimum_History_Count of applied Transaction_Events and a Regime_Shift as the current candidate change point, THE Drift_Sensor SHALL emit a Mass_Function with mass on {LEGIT} of at least the configured regime-shift mass (default 0.4), mass on {FRAUD} of 0, and the remaining mass as Uncertainty_Mass, until a new candidate change point is recorded.
7. FOR ALL synthetic accounts whose behaviour switches to a new stationary regime, THE Change_Point_Detector SHALL classify the switch as a Regime_Shift no later than the Regime_Confirmation_Count plus 5 Transaction_Events after the switch.
8. WHEN a new candidate change point is recorded while an earlier candidate change point is unconfirmed, THE Change_Point_Detector SHALL retain the earlier candidate in the account's change-point history and restart the confirmation count from the new candidate.
9. WHILE an account has fewer than the Minimum_History_Count of applied Transaction_Events or has no current candidate change point, THE Drift_Sensor SHALL emit the Vacuous_Mass_Function.
10. IF the run-length posterior contains a non-finite value, THEN THE Change_Point_Detector SHALL reset the posterior to the prior with no current candidate change point and preserve the account's change-point history, and THE Drift_Sensor SHALL emit the Vacuous_Mass_Function.

### Requirement 10: Streaming Temporal Transaction Graph (REASON)

**User Story:** As a fraud analyst, I want money flows between accounts tracked as a network over time, so that coordinated activity across accounts can be found.

#### Acceptance Criteria

1. WHEN a Transaction_Event completes the REMEMBER stage, THE Temporal_Graph SHALL add exactly one directed edge from the source account node to the destination account node carrying the transaction identifier, timestamp, amount and currency, retaining any existing parallel edges between the same nodes and creating each account node that is not yet present.
2. WHEN a Transaction_Event with a timestamp later than the latest processed timestamp is processed, THE Temporal_Graph SHALL set the latest processed timestamp to that timestamp and evict every edge whose timestamp is strictly earlier than the latest processed timestamp minus the Graph_Window.
3. WHEN an edge has a timestamp exactly equal to the latest processed timestamp minus the Graph_Window, THE Temporal_Graph SHALL retain the edge.
4. THE Temporal_Graph SHALL contain only edges whose timestamps are greater than or equal to the latest processed timestamp minus the Graph_Window (invariant).
5. WHEN an account node has no remaining edges, THE Temporal_Graph SHALL remove the account node before the next Transaction_Event is processed.
6. WHEN queried with an account identifier, a hop depth from 1 to 6 and an inclusive timestamp range, THE Temporal_Graph SHALL return the nodes and edges reachable from the account within the hop depth, traversing edges in both directions and using only edges whose timestamps fall within the range.
7. WHEN the Temporal_Graph is queried, THE Temporal_Graph SHALL leave its nodes, edges and latest processed timestamp unchanged.
8. WHEN queried with an account identifier that has no node in the Temporal_Graph, THE Temporal_Graph SHALL return an empty subgraph.
9. IF a Transaction_Event has a timestamp strictly earlier than the latest processed timestamp minus the Graph_Window, THEN THE Temporal_Graph SHALL add no edge for the Transaction_Event and leave its nodes, edges and latest processed timestamp unchanged.
10. IF a Transaction_Event has the same transaction identifier as an edge already present in the Temporal_Graph, THEN THE Temporal_Graph SHALL add no edge for the Transaction_Event.
11. IF a query specifies a hop depth outside the range 1 to 6 or a timestamp range whose start is later than its end, THEN THE Temporal_Graph SHALL reject the query with an error identifying the invalid parameter.

### Requirement 11: Temporal Motif Detection

**User Story:** As an anti-money-laundering analyst, I want suspicious patterns spanning several transactions detected, so that layering and mule activity is caught even when each transaction looks normal.

#### Acceptance Criteria

1. WHEN an edge is added to the Temporal_Graph, THE Motif_Detectors SHALL evaluate only candidate Motif instances that include the new edge, using all edges present in the Temporal_Graph within the Graph_Window.
2. WHEN a new edge completes a Round_Trip_Cycle, THE Motif_Detector for Round_Trip_Cycle SHALL emit a Motif instance listing the cycle accounts in traversal order, the edge transaction identifiers in timestamp order and the duration equal to the last edge timestamp minus the first edge timestamp.
3. WHEN a new edge causes an account to meet the Fan_Out or Fan_In definition, THE corresponding Motif_Detector SHALL emit a Motif instance listing the hub account, the distinct counterparty accounts, the transaction identifiers and the window start and end timestamps, with the window end minus the window start at most 24 hours.
4. WHEN a new edge causes a source account to meet the Structuring definition, THE Motif_Detector for Structuring SHALL emit a Motif instance listing the transaction identifiers, the Reporting_Threshold and the amounts, with every amount in [0.9 × Reporting_Threshold, Reporting_Threshold) and all listed transactions within 48 hours.
5. WHEN a new edge extends a path to meet the Mule_Chain definition, THE Motif_Detector for Mule_Chain SHALL emit a Motif instance listing the chain accounts in flow order, a forwarding ratio of at least 0.8 for each intermediate account and a hop delay of at most 60 minutes for each hop.
6. WHEN a Motif instance is emitted, THE Motif_Detector SHALL send to the Evidence_Fuser, for each participating Transaction_Event, a Mass_Function with mass 0 on {LEGIT}, the configured per-Motif-type mass in (0, 1] on {FRAUD} and the remaining mass as Uncertainty_Mass, and attach the Motif instance to the Evidence_Chain.
7. FOR ALL emitted Round_Trip_Cycle instances, the cycle SHALL contain 2 to 6 distinct accounts, edges with strictly increasing timestamps and a duration of at most 72 hours (invariant).
8. FOR ALL Transaction_Event sequences of up to 200 events containing an injected Motif instance of a built-in type that satisfies the type's glossary definition and during which no search truncation occurs, THE Motif_Detectors SHALL emit at least one Motif instance of that type whose transaction identifiers include every injected transaction identifier, as checked against a brute-force reference detector (model-based property).
9. FOR ALL emitted Motif instances, the instance SHALL satisfy the glossary definition of its Motif type, as checked against the brute-force reference detector (soundness property).
10. IF a Motif search for one new edge exceeds the configured per-edge search budget (default 20 ms, range 1 to 1,000 ms), THEN THE Motif_Detector SHALL stop the search, emit the Motif instances found so far, record the truncation with the Motif type in the Evidence_Chain, retain the edge in the Temporal_Graph and continue processing the next Transaction_Event.
11. THE Motif_Detectors SHALL emit each combination of Motif type and edge transaction identifier set at most once.
12. IF the configured per-edge search budget is outside the range 1 to 1,000 ms at startup, THEN THE HELIX SHALL refuse to start and report an error naming the per-edge search budget setting.

### Requirement 12: Motif-Aware Case Grouping

**User Story:** As an investigator, I want all transactions in one coordinated scheme gathered into one case, so that I investigate the scheme once rather than piece by piece.

#### Acceptance Criteria

1. WHEN a Motif instance includes Transaction_Events belonging to exactly one open Case (a Case whose status is not CLOSED), THE Case_Manager SHALL add the Motif instance's remaining Transaction_Events and accounts to that Case and append the Motif instance to the Case's Evidence_Chain.
2. WHEN a Motif instance includes Transaction_Events belonging to two or more open Cases, THE Case_Manager SHALL merge those Cases into the surviving Case, containing the union of their Transaction_Events, accounts and Evidence_Chain entries plus the Motif instance and its Transaction_Events and accounts, with no duplicate Transaction_Events or Evidence_Chain entries.
3. WHEN a Motif instance includes no Transaction_Event belonging to an open Case, THE Case_Manager SHALL create a new Case containing the Motif instance's Transaction_Events and accounts, with the Motif instance in the Case's Evidence_Chain.
4. WHEN Cases are merged, THE Case_Manager SHALL select as the surviving Case the Case with the earliest creation time, breaking ties by the lowest Case identifier.
5. WHEN Cases are merged, THE Case_Manager SHALL set the status of each non-surviving Case to CLOSED with a reference to the surviving Case identifier and remove each non-surviving Case from the Active_Queue and the Backlog.
6. FOR ALL sequences of Case add, merge and create operations, each Transaction_Event SHALL belong to at most one open Case after every operation (invariant).
7. WHEN a Case is changed by an add, merge or create operation, THE Case_Manager SHALL recompute the Case's Risk_Score, Expected_Loss, Case_Priority and Severity_Tier before the Prioritiser next ranks Cases.

### Requirement 13: Risk Scoring and Severity Tiers

**User Story:** As an investigator, I want a clear level of concern on each case, so that I can tell at a glance which cases are urgent.

#### Acceptance Criteria

1. THE HELIX SHALL compute the Risk_Score of a Transaction_Event as the Belief plus 0.5 × the Uncertainty_Mass of the Transaction_Event's Fused_Assessment, so that the Vacuous_Mass_Function yields a Risk_Score of 0.5.
2. WHEN a Case has one or more attached Motif instances, THE HELIX SHALL compute the Case Risk_Score as the maximum of the highest Transaction_Event Risk_Score in the Case and the Belief plus 0.5 × the Uncertainty_Mass of the Mass_Function obtained by combining the Fused_Assessment of the highest-scoring Transaction_Event with the Mass_Functions of the attached Motif instances.
3. WHEN a Case has no attached Motif instance, THE HELIX SHALL compute the Case Risk_Score as the highest Transaction_Event Risk_Score in the Case.
4. WHEN a Transaction_Event has a Risk_Score greater than or equal to the configured alerting threshold (default 0.6, range [0, 1]) and an open Case (a Case whose status is not CLOSED) contains the Transaction_Event's source account, THE Case_Manager SHALL add the Transaction_Event to the most recently updated such open Case.
5. WHEN a Transaction_Event has a Risk_Score greater than or equal to the alerting threshold and no open Case contains the Transaction_Event's source account, THE Case_Manager SHALL create a new Case for the Transaction_Event.
6. WHEN a Transaction_Event has a Risk_Score below the alerting threshold, THE Case_Manager SHALL leave Case membership unchanged on the basis of that Risk_Score.
7. THE Case_Manager SHALL assign each Case the Severity_Tier whose configured Risk_Score band contains the Case Risk_Score, where each band includes its lower bound and excludes its upper bound except CRITICAL, which includes 1.0 (defaults: LOW [0.6, 0.7), MEDIUM [0.7, 0.8), HIGH [0.8, 0.9), CRITICAL [0.9, 1.0]), so that a Risk_Score of exactly 0.7 is MEDIUM.
8. FOR ALL pairs of Risk_Scores s1 and s2 in [alerting threshold, 1.0] with s1 ≤ s2, the Severity_Tier assigned to s2 SHALL be equal to or more severe than the Severity_Tier assigned to s1, under the order LOW < MEDIUM < HIGH < CRITICAL (monotonicity property).
9. WHEN a Transaction_Event or Motif instance is added to a Case, THE Case_Manager SHALL recompute the Case Risk_Score before the next Transaction_Event is processed.
10. WHEN a Case's Risk_Score changes, THE Case_Manager SHALL recompute the Case's Severity_Tier and publish the Case identifier, Risk_Score and Severity_Tier to the API_Server within 1 second.
11. IF the configured Severity_Tier bands overlap, leave a gap, are out of order or do not together cover [alerting threshold, 1.0], THEN THE Case_Manager SHALL refuse to start and report an error naming the Severity_Tier band settings.
12. IF the configured alerting threshold is outside the range [0, 1] at startup, THEN THE HELIX SHALL refuse to start and report an error naming the alerting threshold setting.

### Requirement 14: Alert-Budget Prioritisation (ACT)

**User Story:** As a fraud operations lead, I want the alert volume capped at what my team can actually investigate, so that investigators focus on the highest-value cases and are not overwhelmed by false alarms.

#### Acceptance Criteria

1. THE Prioritiser SHALL order the Active_Queue and the Backlog by Case_Priority in descending order, breaking ties by earlier Case creation time.
2. WHEN a Case_Priority changes, THE Prioritiser SHALL reflect the new order in the Active_Queue and the Backlog within 1 second.
3. THE Prioritiser SHALL divide time into consecutive, non-overlapping budget periods of the configured duration (default 60 minutes, range 1 minute to 24 hours), with the first budget period starting at HELIX startup.
4. THE Prioritiser SHALL hold in the Active_Queue at most Alert_Budget Cases admitted during any one budget period, excluding from that count Cases displaced to the Backlog (invariant).
5. WHEN a Case has a Case_Priority greater than 0 and fewer than Alert_Budget Cases admitted during the current budget period are in the Active_Queue, THE Prioritiser SHALL admit the Case to the Active_Queue within 1 second.
6. WHEN a Case with a Case_Priority greater than 0 arrives while Alert_Budget Cases admitted during the current budget period are in the Active_Queue and the lowest-priority Case not yet opened by an Investigator has a Case_Priority strictly lower than the new Case, THE Prioritiser SHALL move that lowest-priority unopened Case to the Backlog, choosing the later-created Case among equal-priority candidates, and admit the new Case to the Active_Queue.
7. WHEN a Case with a Case_Priority greater than 0 arrives while Alert_Budget Cases admitted during the current budget period are in the Active_Queue and no unopened Case in the Active_Queue has a Case_Priority strictly lower than the new Case, THE Prioritiser SHALL place the new Case in the Backlog.
8. WHILE a Case has a Case_Priority less than or equal to 0, THE Prioritiser SHALL keep the Case in the Backlog.
9. WHEN a new budget period begins, THE Prioritiser SHALL admit Backlog Cases with a Case_Priority greater than 0 to the Active_Queue in Case_Priority order until Alert_Budget Cases have been admitted during the new budget period or no such Backlog Case remains.
10. WHEN the Alert_Budget is changed to a value from 1 to 1,000 or the Investigation_Cost is changed to a value from 0 to 1e9 through the API_Server, THE Prioritiser SHALL apply the new value from the next budget period, record the setting name, old value, new value and change time in Storage, and leave the current Active_Queue and Backlog unchanged.
11. IF a requested Alert_Budget or Investigation_Cost value is outside its permitted range, THEN THE Prioritiser SHALL reject the change with an error stating the permitted range and keep the current value.
12. WHEN a budget period ends, THE Evaluation_Monitor SHALL report the number of Cases admitted during the period, the Backlog size, and the captured fraud fraction equal to the Ground_Truth_Store fraud amount in Cases admitted to the Active_Queue divided by the Ground_Truth_Store fraud amount ingested to date.
13. IF the Ground_Truth_Store fraud amount ingested to date is 0 when a budget period ends, THEN THE Evaluation_Monitor SHALL report the captured fraud fraction as "not applicable".
14. WHEN a Case contains a Canary reference, THE Prioritiser SHALL apply the admission, Alert_Budget count and displacement exception defined in Requirement 33.
15. IF the configured budget period duration is outside the range 1 minute to 24 hours at startup, THEN THE HELIX SHALL refuse to start and report an error naming the budget period setting.

### Requirement 15: Case Evidence Chain

**User Story:** As an investigator, I want to see exactly which evidence produced an alert, so that I can verify the reasoning before acting.

#### Acceptance Criteria

1. WHEN a contributor contributes evidence for a Transaction_Event in a Case, THE Case_Manager SHALL record exactly one Evidence_Chain entry for that contributor and Transaction_Event, containing the contributor identifier, the contributor type (Sensor_Agent, Motif_Detector, Antibody, Detection_Rule, Indicator or Canary), the Transaction_Event identifier, the undiscounted Mass_Function, the Sensor_Weight applied and the input features.
2. WHEN a Case is updated, THE Case_Manager SHALL append new entries to the Evidence_Chain and retain every earlier entry unchanged.
3. WHEN Cases are merged, THE Case_Manager SHALL produce an Evidence_Chain that is the union of the merged Evidence_Chains, containing at most one entry per contributor and Transaction_Event pair and retaining every Motif, change-point, missing-field and sensor-failure record.
4. THE Evidence_Chain SHALL include the Fused_Assessment of each contributing Transaction_Event (fused masses, Belief, Plausibility, Uncertainty_Mass and Conflict_Coefficient), the contributing Transaction_Event identifiers, each Motif instance with its Motif type and transaction identifiers, each change point with its classification, one missing-field record per absent Optional_Field per Transaction_Event, and one sensor-failure record per Sensor_Agent failure.
5. THE Case_Manager SHALL order Evidence_Chain entries by Transaction_Event timestamp ascending, then by Transaction_Event identifier, then by contributor identifier.
6. FOR ALL Cases, recomputing the Fused_Assessments from the undiscounted Mass_Functions and Sensor_Weights stored in the Evidence_Chain SHALL reproduce every stored Fused_Assessment value within an absolute tolerance of 1e-9 (reproducibility property).
7. FOR ALL Evidence_Chains, serializing to JSON then deserializing SHALL produce an Evidence_Chain equal field by field, with the same entry order and exactly equal numeric values (round-trip property).
8. IF a serialized Evidence_Chain contains malformed JSON, a missing required field or a mass that violates the Mass_Function definition, THEN THE Case_Manager SHALL reject the Evidence_Chain with an error identifying the offending field, produce no partial Evidence_Chain and leave the data in Storage unchanged.

### Requirement 16: Deterministic Explanations

**User Story:** As an investigator, I want plain-language reasons for every alert that are traceable to evidence, so that I understand why activity was flagged and can justify my decision.

#### Acceptance Criteria

1. WHEN a Case is displayed, THE Explanation_Engine SHALL render reasons only from contributors whose discounted mass on {FRAUD} is greater than their discounted mass on {LEGIT}, using only values stored in the Case's Evidence_Chain.
2. FOR ALL Evidence_Chains, THE Explanation_Engine SHALL produce identical output for identical Evidence_Chains, regardless of wall-clock time, HELIX restart or entry insertion order (determinism property).
3. THE Explanation_Engine SHALL order reasons by discounted mass on {FRAUD} in descending order, breaking ties by contributor name in ascending order.
4. THE Explanation_Engine SHALL attach to each reason the contributor name, the Sensor_Weight, the contributing Transaction_Event identifiers and the comparison values for the contributor type.
5. THE Explanation_Engine SHALL render comparison values as the observed value and Behavioural_DNA baseline for a Sensor_Agent, the Motif type and accounts for a Motif_Detector, the Antibody identifier and matched features for an Antibody, the rule identifier and version for a Detection_Rule, and the Indicator value and confidence for an Indicator.
6. THE Explanation_Engine SHALL include a summary line stating the Severity_Tier, Risk_Score, Belief, Plausibility, Uncertainty_Mass and Conflict_Coefficient to 3 decimal places, and amounts to 2 decimal places followed by the currency.
7. WHEN the Evidence_Chain contains contributors whose discounted mass on {LEGIT} is greater than their discounted mass on {FRAUD}, THE Explanation_Engine SHALL list those contributors as mitigating factors in a separate list, ordered by discounted mass on {LEGIT} in descending order with ties broken by contributor name in ascending order.
8. WHEN the Evidence_Chain contains contributors whose discounted mass on {FRAUD} equals their discounted mass on {LEGIT}, including contributors that emitted the Vacuous_Mass_Function, THE Explanation_Engine SHALL list those contributors as insufficient-evidence factors naming the missing field or missing history.
9. IF a contributor type has no specific template, THEN THE Explanation_Engine SHALL render the contributor with a generic template showing the contributor name, the masses on {FRAUD}, {LEGIT} and {FRAUD, LEGIT}, the Sensor_Weight and the Transaction_Event identifiers.
10. IF a Case's Evidence_Chain is empty or absent, THEN THE Explanation_Engine SHALL render the summary line from the Case's stored Fused_Assessment together with a "no contributing evidence" message, so that the Case remains displayable.

### Requirement 17: Related Accounts Subgraph and Replay Timeline

**User Story:** As an investigator, I want to explore connected accounts and replay how a case unfolded, so that I can investigate relationships between connected events.

#### Acceptance Criteria

1. WHEN an Investigator opens a Case, THE API_Server SHALL return within 2 seconds the Related_Subgraph for the Case accounts, built from edges within the Graph_Window up to the configured hop depth (range 1 to 4, default 2), with each edge carrying its transaction identifier, timestamp and amount and each node carrying an is-case-account flag. The Related_Subgraph hop depth range (1 to 4) is a subset of the Temporal_Graph query hop depth range (1 to 6) defined in Requirement 10 criterion 6.
2. WHEN an Investigator expands an account node in the returned Related_Subgraph, THE API_Server SHALL return the account's direct neighbours and connecting edges within the Graph_Window, with the same edge and node attributes as the Related_Subgraph.
3. WHEN the Related_Subgraph would exceed the configured maximum node count (range 50 to 500, default 500), THE API_Server SHALL return the Case accounts plus the nodes nearest by hop distance up to the maximum node count, flag the result as truncated and report the number of omitted nodes.
4. WHEN an Investigator opens a Case, THE API_Server SHALL return the Replay_Timeline spanning from the earliest contributing Transaction_Event timestamp minus the configured lead time (range 0 to 168 hours, default 24 hours) to the Case's last update, containing every Transaction_Event involving a Case account in that span, ordered by timestamp then transaction identifier, with each Transaction_Event's Sensor_Agent Mass_Functions and Fused_Assessment.
5. WHEN an Investigator selects a timestamp T within the Replay_Timeline, THE API_Server SHALL return within 1 second the Related_Subgraph built from edges with timestamps in [T − Graph_Window, T] and the Fused_Assessments of Transaction_Events with timestamps at or before T, excluding all data with timestamps later than T.
6. IF a request references an unknown Case identifier or an account that is not in the returned Related_Subgraph, THEN THE API_Server SHALL return an error identifying the invalid identifier and leave all HELIX state unchanged.
7. IF a selected timestamp T is outside the Replay_Timeline span, THEN THE API_Server SHALL return an error reporting the valid start and end timestamps of the Replay_Timeline.
8. FOR ALL Cases, replaying the Replay_Timeline Transaction_Events in order from the stored Behavioural_DNA snapshots, Change_Point_Detector states and Sensor_Weights at the timeline start SHALL reproduce every stored Fused_Assessment value within an absolute tolerance of 1e-9 (replay property).
9. IF the configured hop depth (range 1 to 4), maximum node count (range 50 to 500) or lead time (range 0 to 168 hours) is outside its range at startup, THEN THE HELIX SHALL refuse to start and report an error naming each out-of-range setting.

### Requirement 18: Investigator Case Workflow

**User Story:** As an investigator, I want to claim, review and close cases, so that my team can work the queue without duplicating effort.

#### Acceptance Criteria

1. THE Case_Manager SHALL assign each Case exactly one status from OPEN, IN_REVIEW and CLOSED.
2. WHEN an Investigator opens an OPEN Case in the Active_Queue, THE Case_Manager SHALL set the Case status to IN_REVIEW, record the Investigator as the assignee together with the open time, and keep the Case in the Active_Queue.
3. WHEN the assignee submits a Feedback_Label of CONFIRMED_FRAUD or FALSE_POSITIVE with a note of 0 to 2,000 characters on an IN_REVIEW Case, THE Case_Manager SHALL set the Case status to CLOSED, store the Feedback_Label, note, submitting Investigator and submission time, and remove the Case from the Active_Queue within 1 second.
4. WHEN an Investigator escalates an OPEN or IN_REVIEW Case, THE Case_Manager SHALL set the Case Severity_Tier to CRITICAL, keep the Case status unchanged, and record the escalation time, the Investigator and the previous Severity_Tier in the Case history.
5. THE Case_Manager SHALL record every Case status change in the Case history with the change time, previous status, new status and actor, appending entries in ascending time order and retaining earlier entries unchanged.
6. IF an Investigator opens an IN_REVIEW Case assigned to a different Investigator, THEN THE Case_Manager SHALL reject the request with an error naming the assignee and leave the Case unchanged.
7. IF a Feedback_Label submission has a label other than CONFIRMED_FRAUD or FALSE_POSITIVE, a note longer than 2,000 characters, a submitter other than the assignee, or a target Case whose status is not IN_REVIEW, THEN THE Case_Manager SHALL reject the submission with an error stating the reason and leave the Case unchanged.
8. IF an Investigator opens or escalates a CLOSED Case, THEN THE Case_Manager SHALL reject the request with an error and leave the Case unchanged.
9. WHEN a Case is escalated or a Feedback_Label is stored, THE Case_Manager SHALL also write the escalation or Feedback_Label to the Audit_Ledger as specified in Requirement 35.

### Requirement 19: Feedback-Driven Adaptation (LEARN)

**User Story:** As a fraud operations lead, I want HELIX to learn from investigator decisions, so that false positives fall and real fraud patterns are weighted more over time.

#### Acceptance Criteria

1. WHEN a Feedback_Label of CONFIRMED_FRAUD is stored for a Case, THE Feedback_Learner SHALL increase by the configured learning rate the Sensor_Weight of each contributor of any type in the Case's Evidence_Chain whose undiscounted mass on {FRAUD} is strictly greater than its undiscounted mass on {LEGIT}, clamped to the upper Sensor_Weight bound, excluding Canary contributors as specified in criterion 12.
2. WHEN a Feedback_Label of FALSE_POSITIVE is stored for a Case, THE Feedback_Learner SHALL decrease by the configured learning rate the Sensor_Weight of each contributor of any type in the Case's Evidence_Chain whose undiscounted mass on {FRAUD} is strictly greater than its undiscounted mass on {LEGIT}, clamped to the lower Sensor_Weight bound, excluding Canary contributors as specified in criterion 12.
3. THE Feedback_Learner SHALL use configurable Sensor_Weight bounds [lower, upper] (default [0.1, 1.0]) and a configurable learning rate (range (0, 0.5], default 0.05), and IF the bounds do not satisfy 0 ≤ lower < upper ≤ 1 or the learning rate is outside (0, 0.5] at startup, THEN THE HELIX SHALL refuse to start and report an error naming each out-of-range setting.
4. FOR ALL sequences of Feedback_Labels and Sensor_Weight rollbacks, every non-Canary Sensor_Weight SHALL lie within the configured Sensor_Weight bounds after every Feedback_Label and after every rollback (invariant).
5. WHEN a Feedback_Label is stored for a Case, THE Feedback_Learner SHALL leave unchanged the Sensor_Weight of each contributor whose undiscounted mass on {FRAUD} is less than or equal to its undiscounted mass on {LEGIT} and of each contributor absent from the Case's Evidence_Chain.
6. FOR ALL Feedback_Labels, each Sensor_Weight SHALL change by at most the configured learning rate as a result of that Feedback_Label (invariant).
7. WHEN a Feedback_Label of FALSE_POSITIVE is stored for a Case, THE Profile_Store SHALL apply each of the Case's Transaction_Events not yet applied to Behavioural_DNA exactly once, in timestamp order, excluding Transaction_Events that reference a Canary, consistent with Requirement 8 criterion 12, and add each applied Transaction_Event's destination account to the source account's known counterparty set.
8. WHEN a Sensor_Weight changes through a Feedback_Label or a rollback, THE Feedback_Learner SHALL record in Storage and in the Audit_Ledger the contributor identifier, old value, new value, change time, and either the triggering Case identifier or a rollback marker.
9. WHEN an Investigator requests a Sensor_Weight rollback to a time T at or after the earliest Sensor_Weight record in Storage (or the first HELIX startup when no record exists) and at or before the current time, THE Feedback_Learner SHALL restore each Sensor_Weight to the latest recorded value at or before T, or to the contributor's initial Sensor_Weight when no recorded value exists at or before T, and retain every stored Feedback_Label.
10. IF a requested rollback time T is earlier than the earliest Sensor_Weight record in Storage (or the first HELIX startup when no record exists) or later than the current time, THEN THE Feedback_Learner SHALL reject the rollback with an error stating the valid time range and leave every Sensor_Weight unchanged.
11. FOR ALL Cases, the Feedback_Learner SHALL apply Sensor_Weight changes for at most one Feedback_Label per Case (invariant).
12. THE Feedback_Learner SHALL exclude Canary contributors from Sensor_Weight learning and rollback, keeping their fixed Sensor_Weight of 1.0 as specified in Requirement 33.
13. WHEN a Sensor_Weight change record has been written, THE Evidence_Fuser SHALL use the new Sensor_Weight for every fusion that starts after the record is written.

### Requirement 20: Adversarial Attacker Agent

**User Story:** As a demo presenter, I want a red-team agent that keeps inventing new fraud patterns, so that HELIX is tested against attacks it has never seen.

#### Acceptance Criteria

1. WHERE the Attacker_Agent is enabled, THE Attacker_Agent SHALL create an initial set of Pattern_Genomes covering all 7 pattern types: Round_Trip_Cycle, Fan_Out, Fan_In, Structuring, Mule_Chain, account takeover and card-testing.
2. WHERE the Attacker_Agent is enabled, WHEN the Attacker_Agent generates an account-takeover Attack_Campaign, THE Attacker_Agent SHALL emit Auth_Events in addition to Transaction_Events, as specified in Requirement 28.
3. WHERE the Attacker_Agent is enabled, THE Attacker_Agent SHALL launch a new Attack_Campaign at the configured launch interval (default 5 minutes of stream time).
4. WHERE the Attacker_Agent is enabled, THE Attacker_Agent SHALL generate each Attack_Campaign from exactly one Pattern_Genome, containing from 1 to the configured maximum number of events (default 200).
5. WHERE the Attacker_Agent is enabled, WHEN all events of an Attack_Campaign have been injected and the configured detection window (default 24 hours of stream time) has elapsed since the campaign's last injected event, THE Attacker_Agent SHALL mark the Attack_Campaign complete and compute its evasion score as the fraction of the campaign's events that were not Detected_Events by the end of the detection window.
6. WHERE the Attacker_Agent is enabled, WHEN an Attack_Campaign completes, THE Attacker_Agent SHALL select the top K Pattern_Genomes by evasion score (default K = 5), breaking ties by lower Pattern_Genome identifier, and derive N child Pattern_Genomes from each selected parent (default N = 3).
7. FOR ALL child Pattern_Genomes, the child SHALL differ from its parent in at least one of amounts, timing, hop count, topology or account selection, SHALL have every parameter within the configured parameter bounds, and SHALL record its parent Pattern_Genome identifier (invariant).
8. WHERE the Attacker_Agent is enabled, THE Attacker_Agent SHALL inject Attack_Campaign events through the Stream_Ingestor in non-decreasing timestamp order, using only fields defined in the Transaction_Event and Auth_Event schemas so that no field marks an event as an attack.
9. WHERE the Attacker_Agent is enabled, THE Attacker_Agent SHALL limit the injected Attack_Campaign events in each stream-hour to at most the configured attack fraction (default 2%) of all events ingested in that stream-hour.
10. WHERE the Attacker_Agent is enabled, THE Attacker_Agent SHALL write a Ground_Truth_Store record containing the event identifier, the FRAUD label, the Attack_Campaign identifier, the Pattern_Genome identifier and the parent Pattern_Genome identifier before injecting each event.
11. WHERE the Attacker_Agent is enabled, IF the Ground_Truth_Store write for an event fails, THEN THE Attacker_Agent SHALL withhold the event from injection, log the failure with the event identifier, and continue with the next event.
12. THE Ground_Truth_Store SHALL deny read access to the Sensor_Agents, Evidence_Fuser, Motif_Detectors, Case_Manager, Prioritiser, Rule_Engine, Indicator_Store and Playbook_Runner, and grant read access only to the Evaluation_Monitor and the Immune_System miss-detection path.
13. WHERE the Attacker_Agent is enabled, WHEN started with a seed value, THE Attacker_Agent SHALL produce an identical sequence of Pattern_Genomes and Attack_Campaigns for the same seed, HELIX_Config and detection outcomes.
14. WHERE the Attacker_Agent is enabled, THE Evaluation_Monitor SHALL report, per Pattern_Genome lineage, the number of completed Attack_Campaigns, the detection rate, and the median latency from each campaign's first event to the campaign's first Case admission to the Active_Queue.
15. WHERE the Attacker_Agent is enabled, WHEN the number of active Pattern_Genomes would exceed the configured active genome cap (default 100), THE Attacker_Agent SHALL retire the Pattern_Genomes with the lowest evasion scores until the cap is met and retain the records of retired Pattern_Genomes in Storage.
16. WHERE the Attacker_Agent is enabled, THE Attacker_Agent SHALL select target accounts and payees without read access to Canary identifiers, as specified in Requirement 33.

### Requirement 21: Artificial Immune System

**User Story:** As a fraud operations lead, I want HELIX to grow new detectors for patterns that slipped through, so that previously unseen fraud is caught on its next appearance.

#### Acceptance Criteria

1. WHERE the Immune_System is enabled, WHEN the configured detection deadline (default 10 minutes of stream time, measured from the event timestamp) elapses for an Attack_Campaign event that is not a Detected_Event, THE Immune_System SHALL record the event as a miss exactly once, with the event identifier, the Attack_Campaign identifier and the miss time.
2. WHERE the Immune_System is enabled, WHEN a miss is recorded or a Feedback_Label of CONFIRMED_FRAUD is stored for a Case whose Risk_Score at creation was below the alerting threshold, THE Immune_System SHALL create the configured number of candidate Antibodies (range 1 to 50, default 10) by cloning the feature-space signature of the missed event or of the Case's Transaction_Events and mutating each signature parameter by at most the configured mutation rate (default ±10% of the parameter value) (clonal selection).
3. WHERE the Immune_System is enabled, THE Immune_System SHALL activate a candidate Antibody only when the Self_Set contains at least the configured minimum number of events (default 1,000) and the candidate's match rate on the Self_Set is strictly below the configured self-match limit (default 0.5%) (negative selection).
4. WHERE the Immune_System is enabled, IF the Self_Set contains at least the configured minimum number of events and a candidate Antibody's self-match rate is at or above the self-match limit, THEN THE Immune_System SHALL discard the candidate and record the candidate identifier, the self-match rate and the discard reason.
5. WHERE the Immune_System is enabled, WHILE the Self_Set contains fewer than the configured minimum number of events, THE Immune_System SHALL hold each new candidate Antibody inactive and evaluate the candidate under criterion 3 when the Self_Set reaches the minimum.
6. WHERE the Immune_System is enabled, IF a candidate Antibody remains held inactive for longer than the configured hold expiry (default 60 minutes of stream time), THEN THE Immune_System SHALL discard the candidate and record the candidate identifier and the reason "expired".
7. THE Immune_System SHALL exclude from the Self_Set every event that references a Canary and every event in a Case labelled CONFIRMED_FRAUD.
8. WHERE the Immune_System is enabled, WHEN an active Antibody matches a Transaction_Event or Motif instance, THE Antibody SHALL emit to the Evidence_Fuser a Mass_Function satisfying the Mass_Function definition, and THE Case_Manager SHALL record the Antibody as a contributor in the Evidence_Chain with its Sensor_Weight.
9. FOR ALL sequences of Antibody activations and retirements, the active Antibody_Pool size SHALL be at most the configured maximum (default 200) (invariant).
10. WHERE the Immune_System is enabled, WHEN a candidate Antibody is activated while the Antibody_Pool is full, THE Immune_System SHALL retire the active Antibody with the lowest affinity, defined as CONFIRMED_FRAUD matches minus FALSE_POSITIVE matches within the configured affinity window (default 24 hours), retiring the earliest-created Antibody among ties.
11. WHERE the Immune_System is enabled, IF an Antibody's FALSE_POSITIVE Feedback_Label count exceeds the configured limit (default 3), THEN THE Immune_System SHALL retire the Antibody, and the retired Antibody SHALL emit no Mass_Function afterwards.
12. WHERE the Immune_System is enabled, THE Immune_System SHALL record for each Antibody a lineage record containing the origin miss or origin Case, the parent Antibody, the creation time, the matches, and the retirement time and reason (pool capacity or false-positive limit), and retain the record after retirement.
13. WHERE the Immune_System is enabled, THE Immune_System SHALL tag each Antibody with the HELIX_Technique identifiers of the origin Attack_Campaign's Pattern_Genome type or, for an Antibody originating from a Case, with the origin Case's HELIX_Technique tags.
14. WHERE the Immune_System is enabled, FOR ALL Pattern_Genomes and seeds, replaying the same Pattern_Genome with the same seed after an Antibody was activated from that campaign's misses SHALL place at least as many events in Cases within the detection deadline as before activation (metamorphic property).
15. THE Immune_System SHALL evaluate Antibodies without read access to the Ground_Truth_Store, restricting Ground_Truth_Store reads to the miss-detection step of criterion 1.

### Requirement 22: Real-Time Dashboard

**User Story:** As an investigator, I want a live visual console, so that I can watch activity, work cases and explore networks and locations.

#### Acceptance Criteria

1. WHILE the Dashboard is connected to the API_Server, THE Dashboard SHALL display a live feed of the 200 most recently published Transaction_Events, newest first, showing each event's transaction identifier, timestamp, source account, destination account, amount, currency and Risk_Score, with each event displayed within 1 second of publication.
2. WHILE the Dashboard is connected to the API_Server, THE Dashboard SHALL display the Active_Queue ordered by Case_Priority with each Case's Severity_Tier, Risk_Score, Uncertainty_Mass, accounts and amount at risk, together with the Backlog count, updating both within 1 second of a published change.
3. WHEN an Investigator selects a Case, THE Dashboard SHALL display within 2 seconds the Explanation_Engine reasons, the Evidence_Chain, and for each contributor a stacked bar of the discounted masses on {FRAUD}, {LEGIT} and Uncertainty_Mass whose segments together span the full bar width.
4. WHEN an Investigator selects a Case, THE Dashboard SHALL render within 3 seconds the Related_Subgraph of up to 500 nodes as a 3D force-directed network supporting rotate, pan, zoom and node selection, visually distinguishing Case accounts, highlighting Motif edges labelled with their Motif type, and showing a truncation indicator when the API_Server flags the Related_Subgraph as truncated.
5. WHEN an Investigator selects a Case, THE Dashboard SHALL render within 3 seconds the Case's Transaction_Events that have location data on a geographic map, with lines connecting each account's consecutive located Transaction_Events in timestamp order, and display the count of Case Transaction_Events omitted from the map for lack of location data.
6. WHEN an Investigator moves the Replay_Timeline control to a timestamp T, THE Dashboard SHALL update the network, map and evidence views within 1 second to show only data with timestamps at or before T.
7. THE Dashboard SHALL provide keyboard-operable and pointer-operable controls to submit Feedback_Labels, escalate Cases, set the Alert_Budget and Investigation_Cost, and start or stop the Synthetic_Generator and the Attacker_Agent.
8. WHEN the API_Server confirms a control action, THE Dashboard SHALL display the confirmed state within 2 seconds of the action.
9. THE Dashboard SHALL display a loop panel showing the SENSE, PERCEIVE, REMEMBER, REASON, ACT and LEARN stages with per-stage throughput and latency, current Sensor_Weights, Antibody_Pool size, and Evaluation_Monitor precision, recall, alerts per hour and detection latency, refreshed at least every 5 seconds.
10. THE Dashboard SHALL convey each Severity_Tier with a text label in addition to colour.
11. THE Dashboard SHALL support full keyboard navigation of the Active_Queue and Case actions, with a visible focus indicator on the focused element.
12. IF the WebSocket connection to the API_Server drops, THEN THE Dashboard SHALL display a disconnected indicator within 2 seconds, retain the last received data, and attempt reconnection starting after 1 second and doubling the delay after each failed attempt up to a maximum of 30 seconds.
13. WHEN the WebSocket connection is re-established, THE Dashboard SHALL clear the disconnected indicator and refresh the displayed data within 2 seconds.
14. IF an entered Alert_Budget or Investigation_Cost value is outside its permitted range defined in Requirement 14, THEN THE Dashboard SHALL reject the input before sending any request, display a message stating the permitted range, and keep displaying the confirmed value.
15. IF the API_Server rejects a control action or no confirmation arrives within 5 seconds, THEN THE Dashboard SHALL display an error message and keep displaying the last confirmed state.
16. WHERE the corresponding feature is enabled, THE Dashboard SHALL provide views for the Kill_Chain_Stage timeline of a Case, the Coverage_Matrix heatmap, the Detection_Rule editor with backtest results, the Playbook_Action approval queue, Canary alerts, the Indicator_Store, Audit_Ledger verification and the SOC_Console.

### Requirement 23: API Server

**User Story:** As a developer, I want a documented API, so that the Dashboard and external stream sources can integrate with HELIX.

#### Acceptance Criteria

1. THE API_Server SHALL expose REST endpoints to ingest a Transaction_Event, list Cases, retrieve a Case, retrieve a Case's Evidence_Chain, Related_Subgraph and Replay_Timeline, submit Feedback_Labels, escalate Cases, read and update HELIX_Config runtime settings, read Evaluation_Monitor metrics, and request a Sensor_Weight rollback.
2. THE API_Server SHALL expose WebSocket channels for live Transaction_Events with Fused_Assessments, Case creations and updates, and loop stage metrics, publishing at least 95% of the items within 1 second of each item being produced, consistent with Requirement 1 criterion 12.
3. THE API_Server SHALL serve, from the running API_Server, an OpenAPI schema covering every REST endpoint with its parameters, request bodies, response bodies and error status codes.
4. IF a request fails validation, THEN THE API_Server SHALL respond with HTTP status 422 and a body listing each invalid field with the reason, and leave all HELIX state unchanged.
5. IF a request references a Case identifier that does not exist, THEN THE API_Server SHALL respond with HTTP status 404 and leave all HELIX state unchanged.
6. WHEN an ingest request for a valid Transaction_Event or Auth_Event is accepted, THE API_Server SHALL respond with the event identifier within 1 second.
7. THE API_Server SHALL publish loop stage metrics on the loop metrics channel at least once per second, containing the event count and mean latency for each HELIX loop stage.
8. WHEN Cases are listed, THE API_Server SHALL return pages of the requested page size (range 1 to 100, default 50) together with a next-page token whenever further Cases remain.
9. IF a Case workflow request conflicts with the Case state, including an assignee mismatch or a target Case that is CLOSED as defined in Requirement 18, THEN THE API_Server SHALL respond with HTTP status 409 and a body stating the conflict, and leave all HELIX state unchanged.
10. WHERE the corresponding feature is enabled, THE API_Server SHALL expose REST endpoints to ingest Auth_Events, retrieve Case Kill_Chain_Stages and HELIX_Technique tags, read the HELIX_Technique_Catalogue and Coverage_Matrix, submit, validate, backtest, activate and list Detection_Rule versions, list, approve and reject pending Playbook_Actions, list Canary alerts, query, import and export Indicators, read and verify the Audit_Ledger, read SOC_Console data and handoff notes, and start Purple_Team_Exercises and retrieve and compare Exercise_Scorecards.

### Requirement 24: Persistence

**User Story:** As a demo presenter, I want state saved to disk, so that a restart does not lose cases, learned profiles or antibodies.

#### Acceptance Criteria

1. WHEN an event completes the ACT stage, THE Storage SHALL persist in DuckDB within 5 seconds the event, its Fused_Assessment, and every Case, Evidence_Chain and Case history entry that the event changed.
2. WHEN a Feedback_Label is stored, a Sensor_Weight changes, an Antibody lifecycle change occurs or a Ground_Truth_Store record is written, THE Storage SHALL persist the change in DuckDB within 5 seconds, storing Sensor_Weight history as append-only records that retain every earlier record unchanged.
3. THE Storage SHALL snapshot Behavioural_DNA and Change_Point_Detector state at the configured snapshot interval (range 5 to 3,600 seconds, default 60 seconds).
4. IF the configured snapshot interval is outside the range 5 to 3,600 seconds, THEN THE HELIX SHALL refuse to start and report an error naming the snapshot interval setting.
5. WHEN HELIX shuts down, THE Storage SHALL write a final Behavioural_DNA and Change_Point_Detector snapshot and flush every pending write before exit.
6. WHEN HELIX starts with an existing Storage file, THE HELIX SHALL restore, before processing the first new event, the Cases with their Active_Queue and Backlog membership and Case histories, the Feedback_Labels, Sensor_Weights, Antibodies and the latest snapshots, each equal to its last persisted value.
7. WHEN HELIX starts with an existing Storage file, THE HELIX SHALL rebuild the Temporal_Graph from stored Transaction_Events within the Graph_Window ending at the latest stored Transaction_Event timestamp, producing a Temporal_Graph equal to the pre-shutdown Temporal_Graph for that window.
8. WHEN HELIX starts with no Storage file, THE Storage SHALL create an empty Storage file and THE HELIX SHALL start with its default initial state.
9. IF the Storage file cannot be opened or its schema does not match the expected schema, THEN THE HELIX SHALL refuse to start, report the reason, and leave the Storage file unmodified.
10. IF 3 consecutive Storage writes fail, THEN THE HELIX SHALL continue processing events, buffer unwritten records in memory, and report a storage-degraded status through the API_Server until a Storage write succeeds.
11. THE Storage SHALL keep Ground_Truth_Store records in a table separate from detection tables, readable only by the Evaluation_Monitor and the Immune_System miss-detection path.
12. THE Storage SHALL persist Auth_Events, Detection_Rule versions with backtest reports, Playbook_Action logs, Indicators, Audit_Ledger entries and Exercise_Scorecards in DuckDB.

### Requirement 25: Throughput and Latency

**User Story:** As a fraud operations lead, I want HELIX to keep up with live traffic, so that the closed loop stays real-time during the demonstration.

#### Acceptance Criteria

1. WHILE the ingestion rate is at or below 200 events per second on the reference demo machine (4-core CPU, 16 GB RAM) with the default HELIX_Config, THE HELIX SHALL complete processing from receipt by the Stream_Ingestor to completion of the ACT stage within 50 ms at the 95th percentile, measured over a run of at least 10 minutes excluding the first 60 seconds of warm-up.
2. WHILE the ingestion rate is at or below 200 events per second, THE HELIX SHALL keep the inbound queue length, sampled at 1 Hz, below the backpressure limit, with no backpressure signal emitted.
3. WHILE the ingestion rate is at or below 200 events per second, THE HELIX SHALL complete the ACT stage for every accepted non-duplicate event, so that once the inbound queue is drained the completed count equals the received non-duplicate count.
4. THE HELIX SHALL record the per-stage latency and the end-to-end latency of every event.
5. THE API_Server SHALL expose the 50th, 95th and 99th percentile latencies per stage and end to end over a 60-second rolling window and since startup, with sample counts, the ingestion rate and the inbound queue length, refreshed at 1 Hz.
6. IF a latency window contains zero samples, THEN THE API_Server SHALL report the window's percentiles as "unavailable" with a sample count of 0 instead of a latency value.
7. IF the rolling 60-second 95th percentile end-to-end latency exceeds 50 ms, THEN THE HELIX SHALL increment the latency breach counter, expose the breach state through the API_Server, and continue processing every event.
8. WHERE cyber-fusion telemetry is enabled, THE HELIX SHALL count Auth_Events together with Transaction_Events toward the 200 events per second ingestion rate of criteria 1 to 3.

### Requirement 26: Optional LLM Case Narrative

**User Story:** As an investigator, I want an optional plain-English case story, so that I can quickly brief colleagues, while the core decision stays deterministic.

#### Acceptance Criteria

1. WHERE the Narrative_Generator is enabled, WHEN an Investigator requests a narrative for a Case, THE Narrative_Generator SHALL produce a narrative of at most 2,000 characters using only the Case's Evidence_Chain and Explanation_Engine reasons as input.
2. FOR ALL narrative requests, the Narrative_Generator input SHALL contain no Ground_Truth_Store data and no data absent from the Case's Evidence_Chain and Explanation_Engine reasons (invariant).
3. WHERE the Narrative_Generator is enabled, THE Dashboard SHALL display generated narratives in a section separate from the Explanation_Engine reasons, labelled as AI-generated.
4. WHERE the Narrative_Generator is disabled, THE Dashboard SHALL hide the narrative request control and display only the Explanation_Engine reasons.
5. THE HELIX SHALL compute identical Risk_Scores, Severity_Tiers, Case_Priority values, Active_Queue and Backlog ordering, and Explanation_Engine reasons whether the Narrative_Generator is enabled or disabled.
6. WHERE the Narrative_Generator is enabled, IF the Narrative_Generator returns an error or exceeds the configured timeout (range 1 to 120 seconds, default 15 seconds), THEN THE Dashboard SHALL display the Explanation_Engine reasons with a notice that the narrative is unavailable, and THE HELIX SHALL leave the Case state unchanged.
7. WHERE the Narrative_Generator is enabled, WHEN a narrative is unavailable, THE Dashboard SHALL provide a retry control that requests the narrative again without reloading the Dashboard.

### Requirement 27: Security Considerations for the Prototype

**User Story:** As a security reviewer, I want the prototype's security limitations made explicit, so that HELIX is not deployed beyond a local demo without the necessary controls.

#### Acceptance Criteria

1. THE API_Server SHALL bind to the address configured in HELIX_Config, defaulting to 127.0.0.1.
2. WHEN the API_Server starts with a non-loopback bind address (any address outside 127.0.0.0/8 and ::1, including 0.0.0.0 and ::), THE API_Server SHALL log, before serving the first request, a warning naming the bind address and stating that no authentication or authorisation is implemented.
3. THE API_Server SHALL allow REST cross-origin requests only from the configured Dashboard origin (default http://localhost:5173), returning that exact origin and never a wildcard in the CORS allow-origin response.
4. IF a WebSocket connection request carries an origin different from the configured Dashboard origin, THEN THE API_Server SHALL reject the connection before sending any data.
5. THE HELIX SHALL obtain built-in data only from the Synthetic_Generator, the Attacker_Agent and the AMLworld Dataset_Loader.
6. THE project documentation SHALL contain a security section stating that HELIX is for demonstration use only with synthetic or public benchmark data, listing authentication, authorisation, encryption at rest and audit controls as required before use with real customer data, stating that the Audit_Ledger is tamper-evident only (detecting modification of recorded entries but not preventing modification by a party with write access to the Storage file), and stating that Playbook_Actions are simulated only.
7. WHERE the Narrative_Generator is enabled with an external LLM endpoint, THE Dashboard SHALL display on every Case view, before any narrative request is sent, a notice naming the external endpoint host and stating that Case evidence is sent to that host.
8. WHERE the Narrative_Generator is disabled, THE HELIX SHALL send no data to any LLM endpoint.
9. THE Rule_Engine SHALL parse Detection_Rule YAML with a safe loader that performs no code execution and no arbitrary object construction.
10. WHEN a STIX 2.1-style JSON bundle is imported, THE Indicator_Store SHALL reject a bundle larger than the configured size limit (default 10 MB) before parsing and validate every accepted bundle against the Indicator schema.
11. THE Playbook_Runner SHALL execute Playbook_Actions only as simulated state changes inside HELIX, and THE HELIX SHALL NOT transmit any Playbook_Action to a bank, payment, identity, messaging or other system external to HELIX.

### Requirement 28: Cyber-Fusion Telemetry

**User Story:** As a fraud analyst, I want login and account-security events fused with payment evidence, so that a payment made straight after an account compromise is recognised as part of the same incident.

#### Acceptance Criteria

1. WHEN a Stream_Source emits an Auth_Event, THE Stream_Ingestor SHALL pass the Auth_Event into the HELIX loop and record the Auth_Event in the account's authentication history.
2. WHEN a JSON document conforming to the Auth_Event schema is provided, THE Auth_Event_Codec SHALL parse the JSON document into an Auth_Event object.
3. IF a JSON document is missing a required Auth_Event field, contains an unknown event type, or contains a malformed timestamp, THEN THE Auth_Event_Codec SHALL return an error identifying the offending field and THE Stream_Ingestor SHALL route the document to the rejected-events log.
4. THE Auth_Event_Codec SHALL format Auth_Event objects into JSON documents conforming to the Auth_Event schema, and FOR ALL valid Auth_Event objects, formatting then parsing SHALL produce an Auth_Event object equal to the original (round-trip property).
5. THE Synthetic_Generator SHALL emit benign Auth_Events for simulated accounts, including a LOGIN_SUCCESS preceding each simulated session of Transaction_Events and LOGIN_FAILURE events for a configurable fraction of sessions (default 2%).
6. WHERE the Attacker_Agent is enabled, THE Attacker_Agent SHALL generate credential-stuffing, SIM-swap and device-takeover attack sequences of Auth_Events and Transaction_Events at a configurable rate and record each sequence in the Ground_Truth_Store as fraudulent, consistent with Requirement 20 criterion 2.
7. WHEN a Transaction_Event completes the PERCEIVE stage, THE HELIX SHALL link the Transaction_Event to every Auth_Event within the Auth_Correlation_Window that has the same account identifier and the same session identifier or device identifier, and record the links in the Evidence_Chain.
8. WHEN a Transaction_Event follows Suspicious_Auth_Activity on the source account within the Auth_Correlation_Window, THE Auth_Sensor SHALL emit a Mass_Function with mass on {FRAUD} greater than 0 that identifies the Suspicious_Auth_Activity Auth_Events.

### Requirement 29: Kill-Chain Reconstruction

**User Story:** As an investigator, I want each case shown as an attack progression, so that I can see how far an attacker has advanced and act first on cases that are already moving money out.

#### Acceptance Criteria

1. THE HELIX SHALL order Kill_Chain_Stages as RECONNAISSANCE < CREDENTIAL_ACCESS < ACCOUNT_TAKEOVER < PAYEE_MANIPULATION < CASH_OUT < LAYERING.
2. WHEN an entry is added to an Evidence_Chain, THE Case_Manager SHALL assign the entry exactly one Kill_Chain_Stage from the configured contributor-to-stage mapping.
3. IF an Evidence_Chain entry's contributor has no configured Kill_Chain_Stage mapping, THEN THE Case_Manager SHALL assign the configured default stage (default ACCOUNT_TAKEOVER) and mark the entry as unmapped.
4. WHEN a Case is displayed, THE Dashboard SHALL list the Case's Kill_Chain_Stages in stage order, each with the earliest timestamp of an Evidence_Chain entry at that stage, and SHALL show the Case's most advanced Kill_Chain_Stage.
5. FOR ALL Cases, the most advanced Kill_Chain_Stage of the Case SHALL equal the maximum Kill_Chain_Stage over the Case's Evidence_Chain entries (invariant).
6. WHEN a Case's most advanced Kill_Chain_Stage is CASH_OUT or LAYERING, THE Prioritiser SHALL multiply the Case's Expected_Loss by the Kill_Chain_Multiplier before computing Case_Priority.
7. FOR ALL Cases, the Case_Priority computed with the Kill_Chain_Multiplier applied SHALL be greater than or equal to the Case_Priority computed without the Kill_Chain_Multiplier (monotonicity property).
8. IF the Kill_Chain_Multiplier is configured below 1, THEN THE HELIX SHALL reject the HELIX_Config change and keep the previous Kill_Chain_Multiplier.

### Requirement 30: Technique Mapping and Coverage Matrix

**User Story:** As a detection engineer, I want every detector mapped to named attack techniques, so that I can see which techniques HELIX covers and where the detection gaps are.

#### Acceptance Criteria

1. THE HELIX_Technique_Catalogue SHALL carry a version identifier and contain HELIX_Techniques with unique identifiers matching the pattern HX-T followed by four digits, covering both FRAUD and CYBER domains.
2. THE HELIX SHALL map each Sensor_Agent, Motif_Detector, Detection_Rule and Antibody to one or more HELIX_Technique identifiers in the HELIX_Technique_Catalogue.
3. IF a contributor mapping references a HELIX_Technique identifier absent from the HELIX_Technique_Catalogue, THEN THE HELIX SHALL reject the mapping and report the unknown identifier.
4. FOR ALL Cases, the HELIX_Technique tags of the Case SHALL equal the union of the HELIX_Technique identifiers mapped to the contributors in the Case's Evidence_Chain (invariant).
5. WHERE the Attacker_Agent is enabled, THE Attacker_Agent SHALL tag each Pattern_Genome with one or more HELIX_Technique identifiers and store the tags in the Ground_Truth_Store.
6. THE Evaluation_Monitor SHALL compute the Coverage_Matrix listing, for each HELIX_Technique, the detecting contributors and the Attacker_Agent detection rate.
7. WHEN a HELIX_Technique has no detecting contributor, THE Coverage_Matrix SHALL flag the HELIX_Technique as a coverage gap.
8. WHEN the Coverage_Matrix is displayed, THE Dashboard SHALL show the HELIX_Technique_Catalogue version and a notice that the catalogue is ATT&CK-inspired and is not official MITRE data.

### Requirement 31: Detection-as-Code Rule Engine

**User Story:** As a detection engineer, I want to write, test and deploy detection rules as code, so that new detections can be added in minutes without a restart or code change.

#### Acceptance Criteria

1. WHEN a Detection_Rule YAML document is submitted through the API_Server or the configured rules directory, THE Rule_Engine SHALL parse and validate the Detection_Rule against the Detection_Rule schema.
2. IF a submitted Detection_Rule is invalid, THEN THE Rule_Engine SHALL reject the Detection_Rule with an error message stating the line number and cause, and keep the previous version of that Detection_Rule active.
3. WHEN a valid Detection_Rule is accepted, THE Rule_Engine SHALL store the Detection_Rule as a new version with a version number one greater than the latest stored version of that Detection_Rule.
4. WHEN a valid Detection_Rule version is accepted, THE Rule_Engine SHALL backtest the version against stored Transaction_Events and Auth_Events from the configured backtest window (default 7 days) and report the hit count and an estimated false-positive count, defined as hits on events outside Cases labelled CONFIRMED_FRAUD, before the version can be activated.
5. WHEN an Investigator activates a backtested Detection_Rule version, THE Rule_Engine SHALL apply the version to subsequent events within 5 seconds without restarting HELIX.
6. WHEN an active Detection_Rule matches an event, THE Rule_Engine SHALL emit the Detection_Rule's Mass_Function to the Evidence_Fuser and record the Detection_Rule identifier and version as a contributor in the Evidence_Chain.
7. THE Rule_Engine SHALL evaluate Detection_Rules without read access to the Ground_Truth_Store, and IF a Detection_Rule references a Ground_Truth_Store field, THEN THE Rule_Engine SHALL reject the Detection_Rule.
8. THE Rule_Engine SHALL format Detection_Rule objects into YAML documents, and FOR ALL valid Detection_Rules, parsing then formatting then parsing SHALL produce a Detection_Rule equal to the original (round-trip property).

### Requirement 32: Response Playbooks (SOAR-Style)

**User Story:** As a fraud operations lead, I want standard responses triggered automatically from case severity and technique, so that containment is consistent while risky actions still need a human decision.

#### Acceptance Criteria

1. WHEN a Response_Playbook definition is loaded, THE Playbook_Runner SHALL validate the definition and reject definitions with an unknown Playbook_Action, Severity_Tier or HELIX_Technique identifier, reporting the offending field.
2. WHEN a Case's Severity_Tier or HELIX_Technique tags satisfy a Response_Playbook trigger, THE Playbook_Runner SHALL schedule the Response_Playbook's Playbook_Actions for that Case in the defined order.
3. THE Playbook_Runner SHALL perform HOLD_TRANSACTION, STEP_UP_AUTH, FREEZE_ACCOUNT and NOTIFY as simulated state changes recorded inside HELIX only.
4. THE HELIX SHALL never contact any real external system (bank, payment, identity provider, messaging or notification service) when performing a Playbook_Action.
5. WHEN a scheduled Playbook_Action belongs to a Case whose Severity_Tier is at or above the Playbook_Approval_Threshold, THE Playbook_Runner SHALL hold the Playbook_Action as pending until an Investigator approves or rejects the Playbook_Action.
6. WHERE dry-run mode is enabled, THE Playbook_Runner SHALL record the Playbook_Actions that would be performed and mark each as DRY_RUN without changing the simulated state.
7. WHEN a Playbook_Action is scheduled, approved, rejected, performed or dry-run, THE Playbook_Runner SHALL append an entry describing the Playbook_Action, Case identifier and actor to the Audit_Ledger.
8. FOR ALL Cases, THE Playbook_Runner SHALL perform each Response_Playbook version at most once per Case, regardless of how many times the Case is updated (idempotence property).

### Requirement 33: Deception Layer

**User Story:** As a fraud operations lead, I want decoy accounts, payees and devices planted in the system, so that any attacker who touches them is caught with near-certainty and almost no false alarms.

#### Acceptance Criteria

1. WHEN HELIX starts with a seed value, THE HELIX SHALL generate a deterministic set of Canary_Accounts, Canary_Payees and Canary_Devices sized by the configured counts (default 10 of each).
2. WHEN a Transaction_Event or Auth_Event references a Canary as an account, payee, destination or device identifier, THE HELIX SHALL emit to the Evidence_Fuser a Mass_Function with mass on {FRAUD} of at least 0.95 and a fixed Sensor_Weight of 1.0 that the Feedback_Learner does not adjust.
3. WHEN a Transaction_Event or Auth_Event references a Canary, THE Case_Manager SHALL create or update a Case for the event during the processing of that event, regardless of the alerting threshold.
4. WHEN a Case contains a Canary reference, THE Prioritiser SHALL admit the Case to the Active_Queue, exclude the Case from the Alert_Budget count and exempt the Case from displacement to the Backlog, as an exception to Requirement 14.
5. THE Synthetic_Generator SHALL exclude Canaries from all legitimate Transaction_Events and Auth_Events.
6. THE Profile_Store and the Immune_System SHALL exclude events that reference a Canary from Behavioural_DNA updates and from the Self_Set.
7. WHERE the Attacker_Agent is enabled, THE Attacker_Agent SHALL select target accounts and payees from a population that includes Canaries without read access to which identifiers are Canaries.
8. THE Evaluation_Monitor SHALL report each Attack_Campaign that referenced a Canary, with the Canary identifiers and the time of first reference.

### Requirement 34: Threat Intelligence Indicators

**User Story:** As a threat-intelligence analyst, I want indicators from confirmed fraud reused and shared, so that a mule account or device seen once is recognised the next time it appears.

#### Acceptance Criteria

1. THE Indicator_Store SHALL store each Indicator with value, type (ACCOUNT, DEVICE, IP or MERCHANT), confidence, source, first_seen, last_seen, expiry and TLP_Marking.
2. WHEN a Feedback_Label of CONFIRMED_FRAUD is stored for a Case, THE Indicator_Store SHALL extract the account identifiers, device identifiers, IP addresses and merchant identifiers in the Case as Indicators with the configured default confidence (default 75), expiry (default 30 days) and TLP_Marking (default TLP:AMBER), updating last_seen for Indicators that already exist.
3. WHEN a Transaction_Event or Auth_Event contains a value matching an unexpired Indicator, THE Indicator_Store SHALL emit to the Evidence_Fuser a Mass_Function with mass on {FRAUD} equal to the configured indicator base mass (default 0.8) multiplied by the Indicator confidence divided by 100, with the remaining mass assigned to Uncertainty_Mass.
4. WHILE the timestamp of an event is later than an Indicator's expiry, THE Indicator_Store SHALL exclude that Indicator from matching against the event.
5. THE Indicator_Store SHALL export Indicators as STIX 2.1-style JSON bundles, and FOR ALL valid Indicators, exporting then importing SHALL produce Indicators equal to the originals (round-trip property).
6. WHEN a STIX 2.1-style JSON bundle is imported, THE Indicator_Store SHALL parse each Indicator object into an Indicator.
7. IF an imported Indicator object has a missing or invalid field, THEN THE Indicator_Store SHALL reject that Indicator object and report the object identifier and each invalid field.
8. WHEN Indicators are exported without an explicit request to include TLP:RED, THE Indicator_Store SHALL exclude Indicators marked TLP:RED from the export.

### Requirement 35: Tamper-Evident Audit Ledger

**User Story:** As a security reviewer, I want a verifiable record of every configuration, learning and response action, so that any later alteration of the record can be detected.

#### Acceptance Criteria

1. WHEN a HELIX_Config change, Feedback_Label, Sensor_Weight change, Detection_Rule change, Playbook_Action event, escalation or Sensor_Weight rollback occurs, THE Audit_Ledger SHALL append an entry recording the index, timestamp, category, actor and payload, accepting append operations only.
2. THE Audit_Ledger SHALL store in each entry the SHA-256 hash of the previous entry, using 64 zero characters for the first entry, and the SHA-256 hash of the entry's canonical serialization including the previous-entry hash.
3. THE Audit_Ledger SHALL maintain a head record containing the index and hash of the latest entry.
4. WHEN verification is requested, THE Audit_Ledger SHALL recompute the hash chain against the head record and report either that the Audit_Ledger is valid or the index of the first broken entry.
5. FOR ALL Audit_Ledgers built only through append operations, verification SHALL report the Audit_Ledger as valid (invariant).
6. FOR ALL Audit_Ledgers and any modification, deletion or reordering of one or more entries, verification SHALL report a broken entry at or before the index of the first affected entry (tamper-evidence property).
7. THE API_Server and the Dashboard SHALL expose the Audit_Ledger verification operation and its result.
8. THE Dashboard SHALL label the Audit_Ledger verification view with a statement that the Audit_Ledger is tamper-evident and not tamper-proof.

### Requirement 36: SOC Operations Console

**User Story:** As a SOC shift lead, I want response-time targets, workload and handoffs visible in one place, so that urgent cases are not missed and work passes cleanly between shifts.

#### Acceptance Criteria

1. THE SOC_Console SHALL run an SLA_Timer for each open Case using configurable time-to-acknowledge and time-to-close targets per Severity_Tier (defaults: CRITICAL 15 minutes and 4 hours, HIGH 1 hour and 24 hours, MEDIUM 4 hours and 72 hours, LOW 24 hours and 7 days).
2. THE SLA_Timer SHALL measure elapsed wall-clock time from Case creation, using the targets of the Case's current Severity_Tier.
3. WHEN an SLA_Timer exceeds its target, THE SOC_Console SHALL display the Case with the text label "SLA BREACHED" and the breached timer name, in addition to any colour indication.
4. THE SOC_Console SHALL display analyst workload as the number of IN_REVIEW Cases per self-declared Investigator name.
5. WHEN an Investigator submits a shift handoff note, THE SOC_Console SHALL store the note with author name and timestamp and list stored notes in reverse chronological order.
6. THE SOC_Console SHALL display the age of each open Case as the elapsed wall-clock time since Case creation.
7. THE SOC_Console SHALL display mean-time-to-detect, measured from the first contributing event timestamp to Case creation, and mean-time-to-respond, measured from Case creation to CLOSED, over a configurable window (default 24 hours).

### Requirement 37: Purple-Team Exercises and Resilience Mode

**User Story:** As a detection engineer, I want to rehearse attacks and failures safely, so that I can measure coverage and degraded behaviour without disturbing live cases.

#### Acceptance Criteria

1. WHEN a Purple_Team_Exercise is started with a set of Attack_Campaigns, THE HELIX SHALL run the Attack_Campaigns against an isolated copy of the Behavioural_DNA, Change_Point_Detector state, Temporal_Graph, Sensor_Weights, Antibody_Pool, Detection_Rules and Indicator_Store.
2. FOR ALL Purple_Team_Exercises, the live Cases, Sensor_Weights, Antibody_Pool, Behavioural_DNA and Ground_Truth_Store SHALL be equal before and after the exercise (isolation property).
3. WHEN a Purple_Team_Exercise completes, THE HELIX SHALL produce an Exercise_Scorecard with per-HELIX_Technique coverage, detection rate, median detection latency, alert volume and false-positive count.
4. THE Storage SHALL store each Exercise_Scorecard, and WHEN two Exercise_Scorecards are compared, THE API_Server SHALL return the difference for each metric and each HELIX_Technique.
5. THE HELIX SHALL start with Resilience_Mode disabled, and WHERE Resilience_Mode is enabled, THE HELIX SHALL inject sensor failures, delayed events and traffic bursts at the configured rates.
6. WHILE Resilience_Mode is active, THE HELIX SHALL continue processing events and substitute the Vacuous_Mass_Function for each failed Sensor_Agent.
7. WHILE Resilience_Mode is active, THE Evaluation_Monitor SHALL record the count of injected sensor failures, delayed events and bursts, and the resulting per-stage latency percentiles and inbound queue length.
8. FOR ALL injected failure patterns in Resilience_Mode, THE Evidence_Fuser SHALL produce Fused_Assessments whose masses are each in [0, 1] and sum to 1 within a tolerance of 1e-9 (invariant).

### Requirement 38: Showcase and Demo Mode

**User Story:** As a hackathon evaluator, I want dedicated views that show the SOC tooling, the attacker-versus-defender loop and the path of a single transaction through HELIX, so that I can understand and verify the whole system in a short guided demo.

#### Acceptance Criteria

1. THE Dashboard SHALL provide separately selectable tabs for a SOC console view, a Red-vs-Blue arena view, a transaction trace view, a live log view and a guided tour mode.
2. THE Dashboard SOC console view SHALL display the Kill_Chain_Stages per Case, the Coverage_Matrix with the HELIX_Technique_Catalogue version and a notice that the catalogue is not official MITRE data, the Detection_Rules with backtest and activate controls, the pending Playbook_Actions with approve and reject controls, the Indicators with a STIX export control, the Audit_Ledger with a Verify control and the label "tamper-evident, not tamper-proof", the SLA_Timers with the text "SLA BREACHED" for each breached SLA_Timer, the shift handoff notes, a Resilience_Mode toggle, and a control to run a Purple_Team_Exercise with the resulting Exercise_Scorecard.
3. THE Dashboard Red-vs-Blue arena view SHALL display the Attacker_Agent's Attack_Campaigns, the evasion mutations applied to each Pattern_Genome, the Attack_Campaigns missed by HELIX, the Antibody_Pool size, the lineage of each Antibody, and the detection rate over time.
4. WHEN a Transaction_Event identifier is requested at the trace endpoint, THE API_Server SHALL return the Transaction_Event's path through SENSE, PERCEIVE, REMEMBER, REASON, ACT and LEARN, containing each Sensor_Agent's Mass_Function, the Fused_Assessment, the Behavioural_DNA comparison, the Motif hits, the Risk_Score, the Severity_Tier and the Case identifier.
5. IF the trace endpoint receives an unknown Transaction_Event identifier, THEN THE API_Server SHALL return HTTP 404 with a descriptive error message.
6. WHEN a Transaction_Event is selected in the transaction trace view, THE Dashboard SHALL render each stage of the trace returned by the API_Server in SENSE, PERCEIVE, REMEMBER, REASON, ACT, LEARN order.
7. THE Dashboard live log view SHALL display, in timestamp order, entries for Transaction_Events ingested, rejected and dropped as duplicates, Cases opened and updated, Antibodies created and retired, Feedback_Labels recorded and Playbook_Actions executed.
8. WHEN the guided tour mode is started, THE Dashboard SHALL walk the evaluator through a scripted sequence of steps across the views that completes within 5 minutes.
9. THE Dashboard SHALL source all data in the showcase views from the API_Server REST and WebSocket endpoints of the running HELIX backend.
10. IF a HELIX feature backing a showcase view is disabled, THEN THE Dashboard SHALL display a message in that view stating that the feature is disabled and continue rendering the remaining views.
11. THE Dashboard showcase views SHALL meet the accessibility criteria of Requirement 22.

### Requirement 39: Dataset Streaming into the Live System (AMLworld)

**User Story:** As a hackathon evaluator, I want the AMLworld dataset streamed through the running HELIX backend, so that the Dashboard shows real benchmark traffic and per-pattern detection results live.

Scope note: Requirement 4 covers the Dataset_Loader itself and the headless `scripts\aml_replay.py` run; this requirement connects the same Dataset_Loader to the live API_Server. The Attacker_Agent remains optional in dataset mode.

#### Acceptance Criteria

1. WHEN the API_Server is started with `--dataset amlworld --trans <path>` and the optional `--patterns <path>`, `--speed max|N` and `--max-rows N` options, THE Dataset_Feeder SHALL load the files with the Dataset_Loader and submit the in-scope Transaction_Events to the Stream_Ingestor of the running API_Server, so that each Transaction_Event passes through every HELIX loop stage and is published on the live event channel.
2. WHILE the Stream_Ingestor signals backpressure, THE Dataset_Feeder SHALL pause submission and retain every unsubmitted Transaction_Event until the backpressure signal clears.
3. THE Dataset_Feeder SHALL submit Transaction_Events to the Stream_Ingestor in non-decreasing timestamp order with ties in CSV row order, applying the `--max-rows` limit and the speed rules of Requirement 4.
4. THE Dataset_Feeder SHALL write AMLworld labels and pattern membership only to the Ground_Truth_Store, before the first Transaction_Event is submitted, and FOR ALL streamed rows the Transaction_Event published on the live event channel SHALL contain no laundering label or pattern field (ground-truth isolation property).
5. WHEN GET /api/v1/dataset is requested, THE API_Server SHALL return the Dataset_Feeder status (DISABLED, LOADING, WARMING_UP, STREAMING, DONE or FAILED), the progress as submitted Transaction_Events over in-scope rows, the rows read, the rows skipped with a count per skip reason, and the dataset files in use.
6. WHEN GET /api/v1/dataset is requested and a pattern file was loaded, THE Evaluation_Monitor SHALL report through the API_Server, for each AMLworld_Pattern type, the pattern count, the detected pattern count, the transaction-level recall and the precision, reporting precision as unavailable for pattern types that map to no Motif type.
7. IF the API_Server is started with both `--dataset` and `--generator`, THEN THE API_Server SHALL refuse to start with an error stating that the two options are mutually exclusive.
8. WHILE the Dataset_Feeder is active, THE API_Server SHALL reject Synthetic_Generator start requests with HTTP 409 and a body stating that dataset streaming is active.
9. IF a file given to `--trans`, `--patterns` or `--mapping` does not exist, cannot be read or has an invalid header, THEN THE API_Server SHALL refuse to start before serving, exit with a non-zero status and print an error naming the path and the reason.
10. WHEN the Demo_Script is run with `-Dataset amlworld -Trans <path>`, THE Demo_Script SHALL start the API_Server with `--dataset amlworld --trans <path>` in place of `--generator`, passing any given `-Patterns`, `-Speed` and `-MaxRows` values.

### Requirement 40: Company Data Onboarding

**User Story:** As a fraud operations lead at a bank, I want to load my own transaction export through a column mapping and warm HELIX up on our history, so that HELIX runs on our data without code changes.

#### Acceptance Criteria

1. WHEN a Column_Mapping file in YAML or JSON is loaded, THE CSV_Adapter SHALL validate the Column_Mapping and report every error with its field path and reason, rejecting a Column_Mapping that has no source for the timestamp, source account, destination account, amount or currency field, names an unknown Transaction_Event field, or gives an invalid timestamp format or time zone.
2. WHEN the API_Server is started with `--dataset csv --mapping <file> --trans <file>`, THE CSV_Adapter SHALL parse each CSV row into a Transaction_Event using the Column_Mapping's column-to-field assignments, timestamp format and time zone, amount decimal and thousands separators, currency column or constant with the optional currency-name table, and the optional channel, merchant category, device, session, latitude and longitude columns, and THE Dataset_Feeder SHALL stream the Transaction_Events as specified in Requirement 39.
3. IF a CSV row cannot be parsed under the Column_Mapping, THEN THE CSV_Adapter SHALL skip the row, record the row number and the reason, and continue with the next row.
4. WHERE the Column_Mapping names a label column, THE CSV_Adapter SHALL write each row's label only to the Ground_Truth_Store, mapping the configured fraud values to FRAUD and the configured legitimate values to LEGIT, and SHALL exclude the label column from the Transaction_Event.
5. THE CSV_Adapter SHALL format Transaction_Events into CSV rows under a Column_Mapping, and FOR ALL valid Column_Mappings and Transaction_Events representable under the Column_Mapping, formatting then parsing SHALL produce a Transaction_Event equal to the original (round-trip property).
6. FOR ALL valid Column_Mappings, dumping the Column_Mapping to YAML or JSON then loading the document SHALL produce a Column_Mapping equal to the original (round-trip property).
7. WHERE `--warmup` is given, THE Dataset_Feeder SHALL run Warm_Up_Mode over all rows, or over the rows with timestamp before `--warmup-until` when that option is given, submitting the rows at maximum speed under backpressure while THE Case_Manager suppresses Case creation, so that Behavioural_DNA, Change_Point_Detector state and the Temporal_Graph are built without Cases, Feedback_Labels or Playbook_Actions.
8. WHEN Warm_Up_Mode completes, THE Dataset_Feeder SHALL re-enable Case creation, stream any remaining rows at the configured speed, and report through GET /api/v1/dataset the warm-up event count, duration, throughput in events per second, and the number of accounts whose Behavioural_DNA reached the Minimum_History_Count.
9. WHEN `scripts\onboard.ps1` is run with a Column_Mapping and a CSV file, THE Onboard_Validator SHALL parse at most the first N rows (default 1,000) without starting the HELIX loop or writing to the Ground_Truth_Store, and print the rows parsed, the rows rejected with a count and example row numbers per reason, the percentage of parsed rows containing each Transaction_Event field, and the timestamp range.
10. THE HELIX SHALL ship a Column_Mapping for AMLworld and a Column_Mapping for a generic bank export, each with a fixture CSV file of at most 50 rows, and FOR ALL AMLworld fixture rows the CSV_Adapter with the AMLworld Column_Mapping SHALL produce the same Transaction_Event or skip reason as the Dataset_Loader (model-based property).

### Requirement 41: Investigator Workload Reduction

**User Story:** As an investigator, I want low-value alerts cleared, a suggested decision on each Case and faster labelling, so that I spend my time on the Cases that matter while I keep the final decision.

#### Acceptance Criteria

1. WHERE Auto_Triage is enabled (`triage.enabled`, default false), WHEN a Case with Severity_Tier LOW and status OPEN that is not escalated and has no Canary reference has had no update for `triage.auto_clear_after_s` of stream time (default 86,400 seconds, range 60 seconds to 30 days), THE Auto_Triage SHALL set the Case status to CLOSED with Closure_Reason AUTO_CLEARED, leave the Feedback_Label empty, remove the Case from the Active_Queue or Backlog, and record the closure in the Case history with actor "system".
2. THE Auto_Triage SHALL withhold AUTO_CLEARED closures from the Feedback_Learner and the Immune_System, and FOR ALL sequences of Case updates, Feedback_Labels and Auto_Triage sweeps, the Feedback_Learner and the Immune_System SHALL receive exactly the stored Feedback_Labels and no AUTO_CLEARED closure (invariant).
3. WHILE the Audit_Ledger exists, WHEN the Auto_Triage closes a Case, THE Auto_Triage SHALL append an Audit_Ledger entry with category CASE_AUTO_CLEARED, the Case identifier, the idle time in seconds and actor "system".
4. WHEN a Case summary is returned or published, THE Label_Suggester SHALL attach a Suggested_Label computed from the Belief of the Case's fused assessment: CONFIRMED_FRAUD with confidence equal to Belief when Belief is at least `triage.suggest_fraud_belief` (default 0.7), FALSE_POSITIVE with confidence equal to 1 minus Belief when Belief is at most `triage.suggest_fp_belief` (default 0.2), and NONE with confidence 0 otherwise, with a reason text stating the Belief, Plausibility and Uncertainty_Mass.
5. THE Dashboard EvidencePanel SHALL display the Suggested_Label, its confidence and its reason with the text "Suggestion only: the investigator decides", and THE Case_Manager SHALL store a Feedback_Label only from an Investigator submission.
6. WHEN POST /api/v1/cases/bulk-feedback is received with an Investigator name, a Feedback_Label, a note and 1 to 50 distinct Case identifiers, THE API_Server SHALL validate every Case against the rules of Requirement 18, treating an OPEN unassigned Case as opened by the Investigator, and SHALL store the Feedback_Label on every Case only when every Case passes validation.
7. IF a bulk feedback request names an unknown Case, a CLOSED Case or a Case assigned to a different Investigator, or has 0, more than 50 or duplicate Case identifiers, THEN THE API_Server SHALL reject the whole request with HTTP 404, 409 or 422 and a body listing each failing Case identifier with its reason, and FOR ALL rejected bulk feedback requests every Case SHALL be unchanged (all-or-nothing property).
8. WHEN an Investigator selects several Cases in the CaseQueue and chooses a bulk label action, THE Dashboard SHALL show a confirmation stating the Case count and the Feedback_Label, submit the bulk feedback request only after confirmation, and announce the result through a live region.
9. WHILE keyboard focus is inside the EvidencePanel and not in a text input, textarea, select or editable element, WHEN the Investigator presses F, P or E without a modifier key, THE Dashboard SHALL perform the confirm-fraud, false-positive or escalate action on the selected Case through the same path as the corresponding button, list the shortcuts in the EvidencePanel, and expose each shortcut on its button through `aria-keyshortcuts`.
10. WHEN GET /api/v1/metrics is requested, THE API_Server SHALL return investigator workload metrics containing the Feedback_Labels stored in the trailing 60 minutes of wall-clock time in total and per Investigator, the total Feedback_Labels, and the AUTO_CLEARED closures in total and in the trailing 60 minutes.

### Requirement 42: Visual Theme and Motion

**User Story:** As a demo viewer and an Investigator, I want a dark, premium and calm dashboard look with smooth motion, so that HELIX is pleasant to use for long sessions and reads clearly in a presentation, without losing accessibility.

#### Acceptance Criteria

1. THE Theme SHALL define these Design_Tokens on `:root` with these starting values: `--hx-bg` #07070b, `--hx-bg-elev` #0d0d14, `--hx-surface` rgba(255,255,255,0.03), `--hx-surface-hover` rgba(255,255,255,0.06), `--hx-border` rgba(255,255,255,0.08), `--hx-border-strong` rgba(255,255,255,0.16), `--hx-text` #ededf2, `--hx-text-muted` #9a9cab, `--hx-accent` #8b5cf6, `--hx-accent-2` #6366f1, `--hx-cyan` #22d3ee, `--hx-fraud` #f43f5e, `--hx-legit` #34d399, `--hx-warn` #f59e0b, `--hx-radius` 14px, `--hx-radius-sm` 8px, `--hx-glow` (a radial violet-to-indigo gradient), `--hx-font` (Inter variable with a system font fallback), `--hx-ease` cubic-bezier(.22,1,.36,1) and `--hx-dur` 420ms, and THE Dashboard SHALL take every colour, radius, font family, easing and duration in its stylesheet from the Design_Tokens.
2. THE Theme SHALL render every Dashboard panel and showcase view container as a Glass_Card on a `--hx-bg` page background with ambient `--hx-glow` radial gradients and a grain overlay of at most 4% opacity, and SHALL render the shell title as a hero heading filled with an accent gradient.
3. WHEN a pointer hovers over a Glass_Card or a button, THE Theme SHALL raise the element by 2 px, change its border to `--hx-border-strong` and show an accent glow, with a transition of `--hx-dur` using `--hx-ease`.
4. WHEN a tab view is mounted, THE Theme SHALL fade and slide in its panels in DOM order with a stagger of 40 ms per panel, and the entrance of the whole view SHALL complete within 800 ms.
5. WHEN the selected tab changes, THE Theme SHALL move a tab indicator from the previous tab to the selected tab with a transition of `--hx-dur` using `--hx-ease`.
6. THE Theme SHALL load the Inter variable font from the @fontsource-variable/inter package bundled with the Dashboard, and THE Dashboard SHALL request fonts, stylesheets, images and scripts only from its own origin.
7. WHILE the user agent reports `prefers-reduced-motion: reduce`, THE Theme SHALL show the final state of every entrance, hover-lift, glow and tab-indicator animation without movement, with each remaining transition lasting at most 1 ms.
8. THE Theme SHALL give body text a contrast ratio of at least 4.5:1, and large text, focus indicators and UI component boundaries a contrast ratio of at least 3:1, against the composited background they are drawn on.
9. THE Dashboard SHALL convey Severity_Tier, Case status, connection status and fraud or legit meaning through text, an icon or a shape in addition to colour.
10. WHEN the Theme is applied, THE Dashboard SHALL pass every existing Vitest test and `vitest-axe` check with no assertion removed or weakened, and THE Theme SHALL add no runtime dependency other than @fontsource-variable/inter pinned to an exact version and SHALL include no WorkOS or AuthKit logo, copy or asset.

### Requirement 43: Workflow Simulation View

**User Story:** As a demo viewer, a judge or a new Investigator, I want an animated 3D simulation of how HELIX handles real transactions, so that I can understand the whole loop, from a customer's payment to an Investigator's label and the learning that follows, in about a minute.

#### Acceptance Criteria

1. THE Dashboard SHALL add a showcase tab with id `workflow` and label "Workflow" that mounts the Workflow_View, and WHEN the Workflow tab is first selected, THE Dashboard SHALL load the Workflow_Scene code and its 3D libraries as a separate bundle chunk that the other tabs do not load.
2. THE Workflow_Scene SHALL render, from primitives built in code, low-poly Sender, Receiver and Investigator figures and a central HELIX core of six Stage_Rings labelled SENSE, PERCEIVE, REMEMBER, REASON, ACT and LEARN, and SHALL move each Sim_Particle along an arc from its Sender into the core and through the SENSE to ACT Stage_Rings in that order.
3. WHEN a Sim_Particle leaves the ACT Stage_Ring with a legit outcome, THE Workflow_Scene SHALL move the Sim_Particle to its Receiver figure drawn in `--hx-legit` as a round shape with a check mark label.
4. WHEN a Sim_Particle leaves the ACT Stage_Ring with a suspicious outcome, THE Workflow_Scene SHALL redraw the Sim_Particle in `--hx-fraud` as a spiked shape and convert the Sim_Particle into a Case_Card, labelled with the Case identifier and Severity_Tier text, that moves to the Investigator figure's desk.
5. WHEN a Case_Card receives a Feedback_Label, THE Workflow_Scene SHALL send a Learning_Pulse from the Investigator figure through the LEARN Stage_Ring to the PERCEIVE Stage_Ring and change the PERCEIVE Stage_Ring's displayed sensor-weight scale by the weight change of that Feedback_Label.
6. WHILE Story_Mode is selected, THE Workflow_View SHALL play the Story_Script as a loop of 60 to 90 seconds at 1x speed covering Alice's takeover (T007, risk 0.973, CRITICAL, Case C-1), Bob's structuring (T010-T012, risk 0.80, HIGH, Case C-2) and the mule chain (T013-T015, risk 0.85, HIGH, Case C-3), with captions whose numbers match the README walkthrough, and without any API_Server request.
7. THE Workflow_View SHALL select Story_Mode when the Workflow_View is first mounted.
8. WHILE Live_Mode is selected and the WebSocket connection is open, THE Live_Adapter SHALL spawn a Sim_Particle for each `/ws/events` frame with the suspicious outcome when the frame's risk is at or above the Alert_Threshold and the legit outcome otherwise, create a Case_Card for each new Case on `/ws/cases`, and create a Learning_Pulse for each stored Feedback_Label, spawning at most 20 Sim_Particles per second and showing the count of frames not drawn.
9. IF Live_Mode is selected while the WebSocket connection is not open, THEN THE Workflow_View SHALL show the text "Live data unavailable: showing the story" and play Story_Mode until the connection opens.
10. WHERE the Red vs Blue overlay is enabled and the Attacker_Agent is running, THE Workflow_Scene SHALL show an attacker figure that injects Sim_Particles marked with a diamond shape and an "attack" label, and SHALL show each active Antibody as a shield on the PERCEIVE Stage_Ring; WHERE the SOC overlay is enabled and the SOC layer is enabled, THE Workflow_Scene SHALL show a Canary decoy node and a "HOLD" label on Case_Cards held by a Playbook_Action.
11. THE Workflow_View SHALL provide play/pause, restart, speed (0.5x, 1x, 2x), mode (Story or Live) and camera preset (overview, core, investigator) controls as native buttons with accessible names and pressed or selected state, operable by keyboard, and THE Workflow_Scene SHALL support camera orbit by mouse drag and touch.
12. WHILE the Workflow_Scene is playing, THE Workflow_View SHALL show a Caption_Panel naming the current Stage_Ring and describing the stage in plain words, a legend of the six stages and of the fraud, legit, Case and attack shapes, and SHALL announce each new step text through an `aria-live="polite"` region.
13. THE Workflow_View SHALL render the 3D canvas with `role="img"` and an `aria-label` describing the scene, and SHALL provide a "Text view" toggle that shows the Text_View with the current step marked in text.
14. WHILE the user agent reports `prefers-reduced-motion: reduce`, THE Workflow_View SHALL open with the Workflow_Scene paused and provide "Previous step" and "Next step" buttons that move the Story_Script by one step.
15. THE Workflow_Scene SHALL draw Sim_Particles with instanced meshes, hold at most Particle_Cap active Sim_Particles, cap the device pixel ratio at 2, and WHILE the Workflow tab is not selected or the document is hidden, stop the render loop and the simulation clock; IF WebGL is unavailable or the Workflow_Scene throws, THEN THE Workflow_View SHALL show the Text_View with the note "3D view unavailable: showing the text view" while the other tabs keep working.
