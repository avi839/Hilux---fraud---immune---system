import { render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { axe } from "vitest-axe";
import { jsonResponse, routeFetch } from "../../test/fixtures";
import { Arena } from "./Arena";
import {
  detectionSeries,
  genomeChain,
  missedCampaigns,
  mutationsOf,
  normaliseAttacker,
  normaliseImmune,
  type CampaignView,
  type GenomeView,
} from "./arenaModel";

const AXE_OPTIONS = { rules: { "color-contrast": { enabled: false } } };

// Documents with exactly the keys of helix/api/immune_routes.py attacker_doc / immune_doc.
const attackerOff = {
  configured: false, available: true, running: false, seed: null, injected_total: 0, injected_tx_total: 0,
  withheld_total: 0, deferred: 0, pending_events: 0, genomes_total: 0, genomes_active: 0, campaigns_total: 0,
  campaigns_completed: 0, campaigns_live: 0, mean_evasion: null, recent_campaigns: [], top_genomes: [],
};

const immuneOff = {
  enabled: false, miss_detection: false, pool_size: 0, pool_max: 50, held: 0, self_set_size: 0, self_set_min: 1000,
  detection_deadline_s: 600, misses: 0, pending_attack_events: 0, triggers: 0, discards: 0, retirements: 0,
  by_status: {}, recent_antibodies: [], recent_misses: [],
};

const camp = (id: string, started: string, evasion: number | null, completed = true, extra: object = {}) => ({
  campaign_id: id, genome_id: "G4", pattern_type: "MULE_CHAIN", started_at: started, events: 5, injected: 5,
  withheld: 0, evasion, completed, window_end: null, ...extra,
});

const attackerOn = {
  ...attackerOff,
  configured: true, running: true, seed: 7, injected_total: 20, injected_tx_total: 20, genomes_total: 6, genomes_active: 6,
  campaigns_total: 4, campaigns_completed: 3, campaigns_live: 1, mean_evasion: 0.5,
  // newest first, as the server sends them
  recent_campaigns: [
    camp("C4", "2025-03-14T10:15:00+00:00", null, false),
    camp("C3", "2025-03-14T10:10:00+00:00", 1.0, true, { genome_id: "G1", pattern_type: "ROUND_TRIP_CYCLE" }),
    camp("C2", "2025-03-14T10:05:00+00:00", 0.5),
    camp("C1", "2025-03-14T10:00:00+00:00", 0.0, true, { withheld: 1, injected: 4 }),
  ],
  top_genomes: [
    { genome_id: "G1", pattern_type: "ROUND_TRIP_CYCLE", parent_id: null, generation: 0, evasion: 1.0, scored_campaigns: 1, retired: false },
    { genome_id: "G4", pattern_type: "MULE_CHAIN", parent_id: "G2", generation: 2, evasion: 0.25, scored_campaigns: 2, retired: false },
    { genome_id: "G2", pattern_type: "MULE_CHAIN", parent_id: "G0", generation: 1, evasion: 0.1, scored_campaigns: 3, retired: false },
  ],
};

const antibody = (id: string, status: string, parent: string | null, extra: object = {}) => ({
  ab_id: id, contributor_id: `antibody:${id}`, status, origin: "miss:T-9", parent_id: parent,
  created_at: "2025-03-14T10:12:00+00:00", activated_at: status === "ACTIVE" ? "2025-03-14T10:13:00+00:00" : null,
  tags: ["MULE_CHAIN"], dims: ["amount_z", "velocity"], self_match_rate: 0.001, matches: 4, last_match_at: null,
  confirmed_fraud_matches: 2, false_positive_matches: 1, ended_at: null, end_reason: null, retired_at: null, retired_reason: null,
  ...extra,
});

const immuneOn = {
  ...immuneOff,
  enabled: true, miss_detection: true, pool_size: 2, held: 1, self_set_size: 1500, misses: 3, pending_attack_events: 4,
  triggers: 1, discards: 1, retirements: 1,
  by_status: { HELD: 1, ACTIVE: 2, RETIRED: 1, DISCARDED: 1 },
  recent_antibodies: [
    antibody("AB3", "ACTIVE", "AB1"),
    antibody("AB2", "RETIRED", null, { ended_at: "2025-03-14T10:14:00+00:00", end_reason: "false_positive_limit" }),
  ],
  recent_misses: [
    { event_id: "T-9", campaign_id: "C3", miss_time: "2025-03-14T10:20:00+00:00" },
    { event_id: "T-8", campaign_id: "C3", miss_time: "2025-03-14T10:20:00+00:00" },
    { event_id: "T-5", campaign_id: "C2", miss_time: "2025-03-14T10:15:00+00:00" },
  ],
};

const metricsWithRecall = { evaluation: { precision: 0.5, recall: 0.75, alerts_per_hour: 2, latency_median_s: 1, latency_mean_s: 1 } };

describe("Arena", () => {
  it("explains that the Attacker_Agent and Immune_System are not configured", async () => {
    const fetchFn = routeFetch({ "/attacker": attackerOff, "/immune": immuneOff, "/metrics": { evaluation: null } });
    const { container } = render(<Arena />);
    await waitFor(() => expect(screen.getByTestId("arena-sides")).toHaveTextContent("Red team (Attacker_Agent): not attached"));
    expect(screen.getByTestId("arena-sides")).toHaveTextContent("Blue team (HELIX + Immune_System): not enabled");
    expect(screen.getByText(/Start the API server with --attacker/)).toBeInTheDocument();
    expect(screen.getByText(/Start the API server with --immune/)).toBeInTheDocument();
    expect(screen.getByTestId("arena-pool")).toHaveTextContent("n/a");
    expect(screen.getByTestId("arena-rate")).toHaveTextContent("n/a");
    expect(screen.getByTestId("arena-recall")).toHaveTextContent("not applicable (no Evaluation_Monitor)");
    expect(screen.queryByRole("table")).not.toBeInTheDocument();

    const urls = fetchFn.mock.calls.map(([u]) => String(u));
    expect(urls).toEqual(expect.arrayContaining(["/api/v1/attacker?recent=200", "/api/v1/immune?recent=200", "/api/v1/metrics"]));

    const results = await axe(container, AXE_OPTIONS);
    expect(results.violations.map((v) => `${v.id}: ${v.help}`)).toEqual([]);
  });

  it("explains when the attacker cannot run without a Synthetic_Generator", async () => {
    routeFetch({ "/attacker": { ...attackerOff, available: false }, "/immune": immuneOff });
    render(<Arena />);
    await waitFor(() => expect(screen.getByText(/needs the Synthetic_Generator/)).toBeInTheDocument());
  });

  it("reports endpoints the server does not have and server errors", async () => {
    routeFetch({ "/immune": () => jsonResponse({ detail: "boom" }, 500) }); // /attacker, /metrics → 404
    render(<Arena />);
    await waitFor(() => expect(screen.getByText("GET /attacker is not available on this server.")).toBeInTheDocument());
    expect(screen.getByRole("alert")).toHaveTextContent("GET /immune failed: Server rejected the request (500): boom");
    expect(screen.getByTestId("arena-recall")).toHaveTextContent("n/a");
  });

  it("shows campaigns, genome mutations, missed campaigns, pool size, antibody lineage and detection over time", async () => {
    routeFetch({ "/attacker": attackerOn, "/immune": immuneOn, "/metrics": metricsWithRecall });
    const { container } = render(<Arena />);
    await waitFor(() => expect(screen.getByTestId("arena-sides")).toHaveTextContent("Red team (Attacker_Agent): running"));
    expect(screen.getByTestId("arena-sides")).toHaveTextContent("Blue team (HELIX + Immune_System): enabled");

    // Scoreboard
    expect(screen.getByTestId("arena-rate")).toHaveTextContent("50.0%");
    expect(screen.getByTestId("arena-missed-count")).toHaveTextContent("2 of 3 scored (1 fully)");
    expect(screen.getByTestId("arena-pool")).toHaveTextContent("2 / 50 active, 1 held");
    expect(screen.getByTestId("arena-recall")).toHaveTextContent("0.750");

    // Attack_Campaigns, newest first, outcome as text
    const campaigns = screen.getByTestId("arena-campaigns");
    const ids = within(campaigns).getAllByRole("rowheader").map((h) => h.textContent);
    expect(ids).toEqual(["C4", "C3", "C2", "C1"]);
    expect(screen.getByTestId("campaign-C4")).toHaveTextContent("In progress (detection window open)");
    expect(screen.getByTestId("campaign-C3")).toHaveTextContent("Missed: 0% of events detected");
    expect(screen.getByTestId("campaign-C2")).toHaveTextContent("Partially missed: 50.0% detected");
    expect(screen.getByTestId("campaign-C1")).toHaveTextContent("Detected: 100% of events");

    // Genome mutations: lineage chain, evasion change vs parent, missing params noted
    expect(screen.getByTestId("genome-G4")).toHaveTextContent("G0 → G2 → G4");
    expect(screen.getByTestId("genome-G4")).toHaveTextContent("+0.150");
    expect(screen.getByTestId("genome-G4")).toHaveTextContent("not reported");
    expect(screen.getByTestId("genome-G1")).toHaveTextContent("seed genome (no parent)");
    expect(screen.getByTestId("arena-params-note")).toBeInTheDocument();

    // Missed campaigns with miss-detector record counts
    const missed = within(screen.getByTestId("arena-missed")).getAllByRole("rowheader").map((h) => h.textContent);
    expect(missed).toEqual(["C3", "C2"]);
    expect(screen.getByTestId("missed-C3")).toHaveTextContent("Fully missed");
    expect(screen.getByTestId("missed-C3")).toHaveTextContent("100.0%");
    expect(within(screen.getByTestId("missed-C3")).getAllByRole("cell").at(-1)).toHaveTextContent("2");
    expect(screen.getByTestId("missed-C2")).toHaveTextContent("Partially missed");

    // Antibody lineage
    expect(screen.getByTestId("arena-pool-detail")).toHaveTextContent("Pool: 2 of 50 active, 1 held candidates");
    expect(screen.getByText("Showing the newest 2 of 5 Antibodies.")).toBeInTheDocument();
    const ab3 = screen.getByTestId("antibody-AB3");
    expect(ab3).toHaveTextContent("ACTIVE");
    expect(ab3).toHaveTextContent("miss:T-9");
    expect(ab3).toHaveTextContent("AB1");
    expect(ab3).toHaveTextContent("amount_z, velocity");
    expect(ab3).toHaveTextContent("2 / 1");
    expect(screen.getByTestId("antibody-AB2")).toHaveTextContent("false_positive_limit");

    // Detection rate over time: a text alternative and a data table, oldest first
    const chart = screen.getByRole("img", { name: /Detection rate over time/ });
    expect(chart).toHaveAccessibleName(expect.stringContaining("3 scored campaigns, oldest to newest. Latest C3: 0.0% detected; running mean 50.0%."));
    const rows = within(screen.getByTestId("arena-detection-table")).getAllByRole("row").slice(1);
    expect(rows.map((r) => r.textContent)).toEqual([
      expect.stringMatching(/^C1.*100\.0%100\.0%$/),
      expect.stringMatching(/^C2.*50\.0%75\.0%$/),
      expect.stringMatching(/^C3.*0\.0%50\.0%$/),
    ]);

    const results = await axe(container, AXE_OPTIONS);
    expect(results.violations.map((v) => `${v.id}: ${v.help}`)).toEqual([]);
  });

  it("refreshes by polling", async () => {
    let n = 0;
    routeFetch({
      "/attacker": () => {
        n += 1;
        return jsonResponse({ ...attackerOn, campaigns_total: 4 + n });
      },
      "/immune": immuneOn,
    });
    render(<Arena pollMs={30} />);
    await waitFor(() => expect(n).toBeGreaterThanOrEqual(3));
    await waitFor(() => expect(screen.getByTestId("arena-score")).not.toHaveTextContent("5 total"));
  });
});

describe("arena view-model", () => {
  const c = (id: string, startedAt: string | null, evasion: number | null, completed = true): CampaignView => ({
    id, genomeId: "G1", patternType: null, startedAt, events: 1, injected: 1, withheld: 0, evasion, completed, windowEnd: null,
  });

  it("normalises the served documents and tolerates junk", () => {
    const a = normaliseAttacker(attackerOn)!;
    expect(a.configured).toBe(true);
    expect(a.running).toBe(true);
    expect(a.campaigns).toHaveLength(4);
    expect(a.genomes.map((g) => g.id)).toEqual(["G1", "G4", "G2"]);
    const im = normaliseImmune(immuneOn)!;
    expect(im.pool).toEqual({ size: 2, max: 50 });
    expect(im.antibodiesTotal).toBe(5);
    expect(im.antibodies[0]).toMatchObject({ id: "AB3", parentId: "AB1", status: "ACTIVE", confirmedFraudMatches: 2 });
    expect(im.recentMisses).toHaveLength(3);

    expect(normaliseAttacker(null)).toBeNull();
    expect(normaliseImmune([1, 2])).toBeNull();
    const junk = normaliseAttacker({ recent_campaigns: [null, 3, { campaign_id: "" }, { campaign_id: "X" }], top_genomes: "x" })!;
    expect(junk.campaigns.map((x) => x.id)).toEqual(["X"]);
    expect(junk.genomes).toEqual([]);
    expect(junk.configured).toBe(false);
    expect(junk.running).toBeNull();
    expect(normaliseImmune({ by_status: {} })!.antibodiesTotal).toBeNull();
  });

  it("orders the detection series by start time and keeps a running mean", () => {
    const s = detectionSeries([c("B", "2025-01-01T00:02:00Z", 0.5), c("A", "2025-01-01T00:01:00Z", 0), c("L", "2025-01-01T00:03:00Z", null, false), c("U", null, 1)]);
    expect(s.map((p) => p.campaignId)).toEqual(["A", "B", "U"]); // unparseable time last; live campaign skipped
    expect(s.map((p) => p.rate)).toEqual([1, 0.5, 0]);
    expect(s.map((p) => p.cumulative)).toEqual([1, 0.75, 0.5]);
    expect(detectionSeries([])).toEqual([]);
  });

  it("lists only completed campaigns with evasion > 0 as missed, newest first", () => {
    const rows = missedCampaigns(
      [c("A", "2025-01-01T00:01:00Z", 0), c("B", "2025-01-01T00:02:00Z", 0.2), c("C", "2025-01-01T00:03:00Z", 1), c("D", "2025-01-01T00:04:00Z", 1, false)],
      [{ eventId: "e1", campaignId: "C", missTime: null }, { eventId: "e2", campaignId: null, missTime: null }],
    );
    expect(rows.map((r) => [r.campaign.id, r.outcome, r.missRecords])).toEqual([
      ["C", "fully missed", 1],
      ["B", "partially missed", 0],
    ]);
  });

  it("builds genome lineage chains and parameter diffs when parameters are served", () => {
    const g = (id: string, parentId: string | null, params: Record<string, number> | null = null): GenomeView => ({
      id, patternType: "MULE_CHAIN", parentId, generation: null, evasion: null, scoredCampaigns: null, retired: false, params,
    });
    const byId = new Map([g("G1", null, { hops: 5, ratio: 0.9 }), g("G2", "G1", { hops: 6, ratio: 0.9 }), g("G3", "G3")].map((x) => [x.id, x]));
    expect(genomeChain("G2", byId)).toEqual(["G1", "G2"]);
    expect(genomeChain("G3", byId)).toEqual(["G3"]); // self-cycle is cut
    expect(genomeChain("G9", byId)).toEqual(["G9"]);
    expect(mutationsOf(byId.get("G2")!, byId)).toEqual([{ param: "hops", from: 5, to: 6 }]);
    expect(mutationsOf(byId.get("G1")!, byId)).toBeNull();
    expect(mutationsOf(g("G5", "G4", { hops: 1 }), byId)).toBeNull(); // parent unknown
  });
});
