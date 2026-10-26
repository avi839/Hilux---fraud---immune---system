import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { axe } from "vitest-axe";
import { dashboard } from "../../store/store";
import { jsonResponse, routeFetch } from "../../test/fixtures";
import { bobDetail, bobEvidence } from "../../test/sampleRun";
import { vizCalls } from "../../test/vizMocks";
import { bobSubgraph, bobTimeline, enableWebGL, muleNeighbourhood, NO_STORAGE } from "../../test/vizFixtures";
import { toMs } from "../../viz/time";
import { Network3D } from "./Network3D";

const AXE_OPTIONS = { rules: { "color-contrast": { enabled: false } } };

function routes(extra: Record<string, unknown> = {}) {
  return routeFetch({
    "/cases/C-2/subgraph": bobSubgraph,
    "/cases/C-2/evidence": bobEvidence,
    "/cases/C-2/timeline": bobTimeline,
    "/cases/C-2/expand": (init: RequestInit | undefined) => {
      const body = JSON.parse(String(init?.body)) as { account: string };
      return body.account === "ACC-MULE-9" ? jsonResponse(muleNeighbourhood) : jsonResponse({ detail: `account ${body.account} not in subgraph` }, 404);
    },
    ...extra,
  });
}

function select() {
  act(() => {
    dashboard.getState().upsertCase(bobDetail);
    dashboard.getState().selectCase("C-2");
  });
}

describe("Network3D", () => {
  it("falls back to an accessible list without WebGL: accounts, motif edges and truncation", async () => {
    routes();
    const { container } = render(<Network3D />);
    expect(screen.getByText(/select a case/i)).toBeInTheDocument();
    const t0 = performance.now();
    select();
    const accounts = await screen.findByRole("region", { name: "Accounts in the related subgraph" });
    expect(performance.now() - t0).toBeLessThan(3_000);

    expect(screen.getByRole("button", { name: "List view" })).toBeDisabled();
    expect(screen.getByText(/3D view unavailable/)).toBeInTheDocument();
    expect(screen.getByTestId("net-truncated")).toHaveTextContent("Truncated: 7 accounts omitted");
    const rows = within(accounts).getAllByRole("row").slice(1) as HTMLTableRowElement[];
    expect(rows.map((r) => r.cells[0]?.textContent)).toEqual(["ACC-BOB", "ACC-SHELL-1", "ACC-SHELL-2", "ACC-SHELL-3", "ACC-MULE-9"]);
    expect(rows[0]).toHaveTextContent("Case account");
    expect(rows[4]).toHaveTextContent("Related");

    // Motif labels appear once the evidence (shared with the map) has loaded.
    const txs = screen.getByRole("region", { name: "Transactions in the related subgraph" });
    await waitFor(() => expect(within(txs).getByText("T010").closest("tr")).toHaveTextContent("STRUCTURING"));
    expect(within(txs).getByText("T020").closest("tr")).not.toHaveTextContent("STRUCTURING");
    expect(screen.getByTestId("net-summary")).toHaveTextContent("5 accounts (4 case) · 4 transactions · motif edges: STRUCTURING");

    const results = await axe(container, AXE_OPTIONS);
    expect(results.violations.map((v) => `${v.id}: ${v.help}`)).toEqual([]);
  });

  it("expands an account from the keyboard with POST /expand and reports errors", async () => {
    const fetch = routes();
    render(<Network3D />);
    select();
    const accounts = await screen.findByRole("region", { name: "Accounts in the related subgraph" });
    const user = userEvent.setup();
    screen.getByRole("button", { name: "Expand ACC-MULE-9" }).focus();
    await user.keyboard("{Enter}");
    expect(await screen.findByRole("status")).toHaveTextContent("Expanded ACC-MULE-9: 2 neighbours, 1 new.");
    const call = fetch.mock.calls.find(([url]) => String(url).endsWith("/cases/C-2/expand"))!;
    expect(call[1]?.method).toBe("POST");
    expect(JSON.parse(String(call[1]?.body))).toEqual({ account: "ACC-MULE-9" });
    // Accounts table only: ACC-CASHOUT is also T021's destination in the transactions table.
    expect(within(accounts).getByText("ACC-CASHOUT").closest("tr")).toHaveTextContent("Related");
    expect(within(accounts).getByText("ACC-MULE-9").closest("tr")).toHaveTextContent("Related, expanded");
    expect(screen.getByText("T021")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Expand ACC-CASHOUT" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not expand ACC-CASHOUT: Server rejected the request (404)");
  });

  it("renders the lazy 3D view with distinct case nodes and labelled motif edges, and expands on node click", async () => {
    enableWebGL();
    const fetch = routes();
    render(<Network3D />);
    select();
    // The lazy chunk is transformed on first import; allow for a loaded parallel test run.
    expect(await screen.findByTestId("mock-force-graph", {}, { timeout: 5_000 })).toBeInTheDocument();
    await waitFor(() => expect(vizCalls.lastForceGraph()?.graphData).toBeDefined());
    await waitFor(() => {
      const p = vizCalls.lastForceGraph()!;
      const links = (p.graphData as { links: Array<{ id: string; motifs: string[] }> }).links;
      expect(links.find((l) => l.id === "T011")?.motifs).toEqual(["STRUCTURING"]);
    });
    const p = vizCalls.lastForceGraph()!;
    const data = p.graphData as { nodes: Array<{ id: string; isCase: boolean }>; links: Array<{ id: string; motifs: string[] }> };
    expect(data.nodes).toHaveLength(5);
    const bob = data.nodes.find((n) => n.id === "ACC-BOB")!;
    const mule = data.nodes.find((n) => n.id === "ACC-MULE-9")!;
    const val = p.nodeVal as (n: unknown) => number;
    const colour = p.nodeColor as (n: unknown) => string;
    expect(val(bob)).toBeGreaterThan(val(mule));
    expect(colour(bob)).not.toBe(colour(mule));
    const motifLink = data.links.find((l) => l.id === "T011")!;
    const plain = data.links.find((l) => l.id === "T020")!;
    expect((p.linkLabel as (l: unknown) => string)(motifLink)).toContain("[STRUCTURING]");
    expect((p.linkColor as (l: unknown) => string)(motifLink)).not.toBe((p.linkColor as (l: unknown) => string)(plain));
    expect((p.linkWidth as (l: unknown) => number)(motifLink)).toBeGreaterThan((p.linkWidth as (l: unknown) => number)(plain));
    expect(screen.getByRole("list", { name: "Network legend" })).toHaveTextContent("Motif edge: STRUCTURING");
    expect(screen.getByRole("img", { name: /3D network of 5 accounts and 4 transactions/ })).toBeInTheDocument();

    act(() => (p.onNodeClick as (n: { id: string }) => void)({ id: "ACC-MULE-9" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Expanded ACC-MULE-9");
    expect(fetch.mock.calls.some(([url]) => String(url).endsWith("/cases/C-2/expand"))).toBe(true);
    await waitFor(() => expect((vizCalls.lastForceGraph()!.graphData as { nodes: unknown[] }).nodes).toHaveLength(6));

    // The toggle switches to the text view and back.
    const user = userEvent.setup();
    const toggle = screen.getByRole("button", { name: "List view" });
    expect(toggle).toHaveAttribute("aria-pressed", "false");
    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("network-list")).toBeInTheDocument();
    expect(screen.queryByTestId("mock-force-graph")).not.toBeInTheDocument();
  });

  it("filters to transactions at or before the replay position and then shows the server frame", async () => {
    const frame = {
      case_id: "C-2",
      t: "2024-06-05T10:00:00+00:00",
      window_start: "2024-06-04T10:00:00+00:00",
      subgraph: { ...bobSubgraph, edges: bobSubgraph.edges.slice(0, 1), nodes: bobSubgraph.nodes.slice(0, 4), truncated: false, omitted: 0 },
      entries: [],
    };
    routes({ "/cases/C-2/timeline/at": frame });
    render(<Network3D />);
    select();
    const txs = await screen.findByRole("region", { name: "Transactions in the related subgraph" });
    await screen.findByText("T012");
    act(() => dashboard.getState().setReplayT(toMs("2024-06-05T12:00:00+00:00")));
    // Immediately (same render): local filter, later transactions hidden.
    expect(within(txs).queryByText("T012")).not.toBeInTheDocument();
    expect(within(txs).getByText("T020")).toBeInTheDocument();
    expect(screen.getByTestId("net-replay")).toHaveTextContent("transactions at or before T, 2 later transactions hidden");
    // After the debounce the server frame replaces the local view.
    await waitFor(() => expect(screen.getByTestId("net-replay")).toHaveTextContent("server frame"));
    expect(within(screen.getByRole("region", { name: "Transactions in the related subgraph" })).queryByText("T020")).not.toBeInTheDocument();
    expect(screen.queryByTestId("net-truncated")).not.toBeInTheDocument();
  });

  it("still shows the live network when the timeline needs Storage (503)", async () => {
    routes({ "/cases/C-2/timeline": () => jsonResponse(NO_STORAGE, 503) });
    render(<Network3D />);
    select();
    expect(await screen.findByRole("region", { name: "Accounts in the related subgraph" })).toBeInTheDocument();
  });

  it("shows an error with a working Retry when the subgraph fails", async () => {
    let fail = true;
    routes({ "/cases/C-2/subgraph": () => (fail ? jsonResponse({ detail: "boom" }, 500) : jsonResponse(bobSubgraph)) });
    render(<Network3D />);
    select();
    const err = await screen.findByTestId("net-error");
    expect(err).toHaveTextContent("Could not load the related subgraph for C-2: Server rejected the request (500): boom");
    fail = false;
    await userEvent.setup().click(within(err).getByRole("button", { name: "Retry" }));
    expect(await screen.findByTestId("net-summary")).toBeInTheDocument();
  });
});
