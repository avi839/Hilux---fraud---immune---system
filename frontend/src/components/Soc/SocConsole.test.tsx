import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { axe } from "vitest-axe";
import { SOC_ABSENT_MESSAGE, SOC_DISABLED_MESSAGE } from "../../api/soc";
import { dashboard } from "../../store/store";
import { jsonResponse, routeFetch } from "../../test/fixtures";
import { SocConsole } from "./SocConsole";

const AXE_OPTIONS = { rules: { "color-contrast": { enabled: false } } };
const TS = "2025-03-14T10:22:05.120+00:00";

const timer = (target: number, elapsed: number, breached: boolean) => ({
  target_s: target,
  elapsed_s: elapsed,
  remaining_s: target - elapsed,
  running: true,
  breached,
});

const consoleDoc = {
  now: TS,
  open_cases: 2,
  breached_cases: 1,
  cases: [
    {
      case_id: "C-1",
      status: "OPEN",
      tier: "CRITICAL",
      assignee: null,
      age_s: 4000,
      breached: true,
      label: "SLA BREACHED",
      breached_timers: ["time_to_acknowledge"],
      timers: { time_to_acknowledge: timer(900, 4000, true), time_to_close: timer(14_400, 4000, false) },
    },
    {
      case_id: "C-2",
      status: "IN_REVIEW",
      tier: "LOW",
      assignee: "ana",
      age_s: 60,
      breached: false,
      label: null,
      breached_timers: [],
      timers: { time_to_acknowledge: timer(86_400, 60, false), time_to_close: timer(604_800, 60, false) },
    },
  ],
  workload: [{ investigator: "ana", in_review: 1 }],
  mttd: { mean_s: 12, cases: 2 },
  mttr: { mean_s: null, cases: 0 },
  handoff_notes: [{ note_id: 1, author: "ana", ts: TS, text: "Watch the mule ring on C-1" }],
};

const coverageDoc = {
  catalogue_version: "helix-tc-1.0",
  notice: "HELIX technique catalogue, not official MITRE ATT&CK data.",
  summary: { techniques: 1, covered: 0, gaps: 1 },
  gaps: ["HX-T0001"],
  techniques: [
    { technique_id: "HX-T0001", name: "Card testing", domain: "payments", contributors: [], detection_rate: null, campaigns: 0, gap: true },
  ],
};

const rulesDoc = {
  rules: [
    {
      rule_id: "HXR-1",
      version: 1,
      title: "Dormant big spender",
      status: "experimental",
      target: "transaction",
      techniques: ["HX-T0014"],
      mass: { fraud: 0.4, legit: 0 },
      latest_version: 1,
      active_version: null,
      versions: [{ version: 1, backtested: true }],
    },
  ],
  eval_errors: 0,
  backtest_window_s: 604_800,
};

const playbooksDoc = {
  playbooks: [],
  dry_run: false,
  approval_threshold: "HIGH",
  actions: {},
  simulated_state: {},
  notice: "All actions are simulated inside HELIX.",
};

const pendingDoc = {
  actions: [{ action_id: "PA-1", case_id: "C-1", type: "FREEZE_ACCOUNT", status: "PENDING", targets: ["A-1"] }],
  count: 1,
};

const auditDoc = {
  total: 1,
  offset: 0,
  limit: 50,
  head: { index: 0, hash: "f00dbabe".repeat(8) },
  entries: [{ index: 0, ts: TS, category: "case", actor: "system", payload: { case_id: "C-1" }, prev_hash: "0".repeat(64), hash: "f00dbabe".repeat(8) }],
};

const resilienceDoc = {
  enabled: false,
  rates: { sensor_failure_rate: 0.1, delay_rate: 0.1, burst_rate: 0.05 },
  counters: { sensor_failures: 0 },
  queue: { length: 0, held: 0 },
  latency_ms: null,
};

const scorecard = {
  scorecard_id: "SC-1",
  name: "Dashboard exercise: 3 patterns",
  created_at: TS,
  metrics: {
    campaigns: 3,
    detected_campaigns: 2,
    campaign_detection_rate: 2 / 3,
    events: 40,
    detected_events: 30,
    detection_rate: 0.75,
    median_detection_latency_s: 42,
    alert_volume: 4,
    false_positives: 1,
  },
  techniques: [],
  campaigns: [{ campaign_id: "K-1", ptype: "FAN_OUT", detected: false, events: 10, detected_events: 0 }],
};

type Call = { url: string; method: string; body: unknown };

/** Every SOC route answering with fixtures; POSTs are recorded in ``calls``. */
function socRoutes(calls: Call[]) {
  const post = (respond: (body: unknown) => unknown, status = 200) => (init: RequestInit | undefined, url: string) => {
    const body = typeof init?.body === "string" ? (JSON.parse(init.body) as unknown) : undefined;
    calls.push({ url, method: init?.method ?? "GET", body });
    return jsonResponse(respond(body), status);
  };
  const getOr = (doc: unknown, onPost: (init: RequestInit | undefined, url: string) => Response) => (init: RequestInit | undefined, url: string) =>
    init?.method === "POST" ? onPost(init, url) : jsonResponse(doc);
  return {
    "/soc/console": consoleDoc,
    "/soc/handoff-notes": post((b) => ({ note_id: 2, ts: TS, ...(b as object) }), 201),
    "/soc/coverage": coverageDoc,
    "/soc/rules": rulesDoc,
    "/soc/rules/HXR-1/versions/1/backtest": post(() => ({ hits: 3, estimated_false_positives: 1, window_s: 604_800 })),
    "/soc/rules/HXR-1/activate": post(() => ({ rule_id: "HXR-1", active_version: 1 })),
    "/soc/playbooks": playbooksDoc,
    "/soc/playbook-actions": pendingDoc,
    "/soc/playbook-actions/PA-1/approve": post((b) => ({ ...pendingDoc.actions[0], status: "APPROVED", decided_by: (b as { by: string }).by })),
    "/soc/indicators": { count: 0, total: 0, offset: 0, limit: 100, indicators: [] },
    "/soc/audit": auditDoc,
    "/soc/audit/verify": { valid: true, first_broken_index: null, entries: 1, reason: null, notice: "Tamper-evident, not tamper-proof." },
    "/soc/resilience": getOr(resilienceDoc, post((b) => ({ ...resilienceDoc, enabled: (b as { enabled: boolean }).enabled }))),
    "/soc/purple/scorecards": { scorecards: [] },
    "/soc/purple/exercises": post(() => scorecard, 201),
  };
}

async function loaded() {
  await screen.findByTestId("sla-summary");
  await screen.findByTestId("coverage-summary");
  await screen.findByTestId("audit-summary");
  await screen.findByTestId("resilience-toggle");
  await screen.findByTestId("action-PA-1");
  await screen.findByTestId("rule-HXR-1");
  await screen.findByText("No exercises have been run yet.");
  await screen.findByText(/No indicators yet/);
}

describe("SocConsole", () => {
  it("shows every Req 38.2 element from the SOC routes", async () => {
    routeFetch(socRoutes([]));
    render(<SocConsole />);
    await loaded();
    // SLA timers: "SLA BREACHED" for the breached one only, plus the handoff notes.
    expect(screen.getByTestId("sla-breached-C-1")).toHaveTextContent("SLA BREACHED");
    expect(within(screen.getByTestId("sla-C-2")).queryByText(/SLA BREACHED/)).toBeNull();
    expect(screen.getByTestId("handoff-notes")).toHaveTextContent("ana: Watch the mule ring on C-1");
    // Coverage: catalogue version and the not-official-MITRE notice.
    expect(screen.getByTestId("coverage-notice")).toHaveTextContent("helix-tc-1.0");
    expect(screen.getByTestId("coverage-notice")).toHaveTextContent("not official MITRE");
    // Audit label, controls for rules, actions, STIX export, verify, resilience and purple team.
    expect(screen.getByTestId("audit-label")).toHaveTextContent("tamper-evident, not tamper-proof");
    expect(screen.getByRole("button", { name: "Backtest HXR-1 version 1" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Activate HXR-1 version 1" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Approve PA-1" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Reject PA-1" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Export STIX" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Verify" })).toBeEnabled();
    expect(screen.getByRole("switch", { name: /Resilience mode off/ })).not.toBeChecked();
    expect(screen.getByRole("button", { name: "Run purple-team exercise" })).toBeEnabled();
  });

  it("drives the controls with the investigator name", async () => {
    const calls: Call[] = [];
    routeFetch(socRoutes(calls));
    render(<SocConsole />);
    await loaded();
    const user = userEvent.setup();

    // Without a name the decision is refused locally and nothing is sent.
    await user.click(screen.getByRole("button", { name: "Approve PA-1" }));
    expect(within(screen.getByTestId("action-PA-1")).getByRole("alert")).toHaveTextContent(/investigator name/i);
    expect(calls).toEqual([]);

    await user.type(screen.getByLabelText(/Investigator name/), "ana");
    await user.click(screen.getByRole("button", { name: "Approve PA-1" }));
    expect(await screen.findByText("PA-1 is now APPROVED.")).toBeInTheDocument();
    expect(calls.at(-1)).toMatchObject({ method: "POST", body: { by: "ana" } });

    await user.click(screen.getByRole("button", { name: "Backtest HXR-1 version 1" }));
    expect(await screen.findByText(/Backtest of HXR-1 v1: 3 hits, 1 estimated false positives/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Activate HXR-1 version 1" }));
    expect(await screen.findByText("HXR-1 version 1 is active.")).toBeInTheDocument();
    expect(calls.at(-1)).toMatchObject({ body: { by: "ana", version: 1 } });

    await user.type(screen.getByLabelText("New shift handoff note"), "Quiet shift");
    await user.click(screen.getByRole("button", { name: "Add note" }));
    expect(await screen.findByText("Handoff note 2 saved by ana.")).toBeInTheDocument();
    expect(calls.at(-1)).toMatchObject({ body: { author: "ana", text: "Quiet shift" } });

    await user.click(screen.getByRole("button", { name: "Verify" }));
    expect(await screen.findByTestId("audit-verify")).toHaveTextContent("Chain verified: 1 entry intact.");

    await user.click(screen.getByRole("switch", { name: /Resilience mode/ }));
    expect(await screen.findByText(/Resilience mode is on/)).toBeInTheDocument();
    expect(calls.find((c) => c.url.endsWith("/soc/resilience"))).toMatchObject({ body: { enabled: true } });

    await user.click(screen.getByRole("button", { name: "Run purple-team exercise" }));
    const card = await screen.findByTestId("scorecard");
    expect(card).toHaveTextContent("SC-1");
    expect(card).toHaveTextContent("75% of events (30/40)");
    expect(card).toHaveTextContent("False positives1");
    expect(card).toHaveTextContent("K-1 FAN_OUT: MISSED");
    const ex = calls.find((c) => c.url.endsWith("/soc/purple/exercises"))!;
    expect(ex.body).toMatchObject({
      campaigns: [
        { ptype: "FAN_OUT", count: 1 },
        { ptype: "STRUCTURING", count: 1 },
        { ptype: "MULE_CHAIN", count: 1 },
      ],
    });
  });

  it("shows the kill chain of the case selected in the store", async () => {
    routeFetch({
      ...socRoutes([]),
      "/cases/C-1/killchain": {
        case_id: "C-1",
        most_advanced: "CASH_OUT",
        stages: [{ stage: "CASH_OUT", first_ts: TS, entries: 2 }],
        entries: [],
        unmapped: [],
        default_stage: "RECONNAISSANCE",
        multiplier: { enabled: true, value: 1.5, applied: true, expected_loss: "150.00", expected_loss_base: "100.00", priority: "150.00" },
      },
      "/cases/C-1/techniques": { case_id: "C-1", catalogue_version: "helix-tc-1.0", notice: "Not MITRE.", techniques: [], contributors: [] },
    });
    act(() => dashboard.getState().selectCase("C-1"));
    render(<SocConsole />);
    expect(await screen.findByTestId("killchain-most")).toHaveTextContent("Cash-out");
    expect(screen.getByTestId("killchain-CASH_OUT")).toHaveAttribute("aria-current", "step");
  });

  it("says when the SOC routes are missing or cyber fusion is off, and keeps the other sections", async () => {
    // Coverage answers 503 (feature disabled); every other SOC route is absent (404 Not Found).
    routeFetch({ "/soc/coverage": () => jsonResponse({ detail: "cyber fusion is disabled" }, 503), "/soc/rules": rulesDoc });
    render(<SocConsole />);
    expect(await screen.findByTestId("soc-disabled-soc-coverage")).toHaveTextContent(SOC_DISABLED_MESSAGE);
    for (const area of ["soc-sla", "soc-playbooks", "soc-intel", "soc-audit", "soc-resilience", "soc-purple"]) {
      expect(await screen.findByTestId(`soc-absent-${area}`)).toHaveTextContent(SOC_ABSENT_MESSAGE);
    }
    expect(await screen.findByTestId("rule-HXR-1")).toBeInTheDocument();
    expect(screen.queryAllByRole("alert")).toEqual([]);
  });

  it("has no axe violations with data loaded", async () => {
    routeFetch(socRoutes([]));
    const { container } = render(<SocConsole />);
    await loaded();
    await waitFor(() => expect(container.querySelector("[aria-busy='true']")).toBeNull());
    const results = await axe(container, AXE_OPTIONS);
    expect(results.violations.map((v) => `${v.id}: ${v.help}`)).toEqual([]);
  });
});
