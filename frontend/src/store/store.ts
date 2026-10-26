// Zustand store (design §22): live feed ring of 200, Cases by id, the derived Active_Queue,
// backlog count, connection state, selected Case, metrics and server-confirmed control values.

import { createStore, useStore } from "zustand";
import type { CaseSummary, CaseUpdatedMessage, MetricsMessage } from "../api/types";
import type { SocketStatus } from "../api/ws";
import {
  activeQueue,
  announcementFor,
  pushEvents,
  upsertCase,
  type Announcement,
  type FeedItem,
} from "./reducers";

export interface ConfirmedControls {
  alertBudget: number | null;
  investigationCost: number | null;
  /** Budget period from which the last confirmed budget/cost change applies. */
  appliesFromPeriod: number | null;
  generatorRunning: boolean | null;
}

/** Demo-only simulated investigator reported under GET /status "sim_investigator". */
export interface SimAnalyst {
  enabled: boolean;
  labelsTotal: number;
  confirmed: number;
  falsePositive: number;
  errorRate: number | null;
  intervalS: number | null;
}

export interface DashboardState {
  feed: FeedItem[];
  cases: Record<string, CaseSummary>;
  queue: string[];
  backlogCount: number;
  connection: SocketStatus;
  selectedCaseId: string | null;
  metrics: MetricsMessage | null;
  confirmed: ConfirmedControls;
  announcement: Announcement | null;
  lastRefresh: number | null;
  /** Replay_Timeline position (epoch ms) shared by the network, map and evidence views;
   *  null = no replay filter (current state). Reset whenever the selection changes. */
  replayT: number | null;
  /** The investigator name typed in Controls (raw; validated by checkInvestigator at use).
   *  Shared so the CaseQueue bulk actions send the same ``by``. */
  investigator: string;
  /** GET /status "sim_investigator"; null when the server does not report it. */
  simAnalyst: SimAnalyst | null;

  applyEvents: (batch: FeedItem[]) => void;
  applyCaseMessage: (m: CaseUpdatedMessage) => void;
  upsertCase: (c: CaseSummary) => void;
  replaceCases: (items: CaseSummary[], backlogCount: number) => void;
  setConnection: (s: SocketStatus) => void;
  selectCase: (id: string | null) => void;
  setMetrics: (m: MetricsMessage) => void;
  setConfirmed: (patch: Partial<ConfirmedControls>) => void;
  setReplayT: (t: number | null) => void;
  setInvestigator: (name: string) => void;
  setSimAnalyst: (s: SimAnalyst | null) => void;
  reset: () => void;
}

const initialData = () => ({
  feed: [] as FeedItem[],
  cases: {} as Record<string, CaseSummary>,
  queue: [] as string[],
  backlogCount: 0,
  connection: "connecting" as SocketStatus,
  selectedCaseId: null as string | null,
  metrics: null as MetricsMessage | null,
  confirmed: { alertBudget: null, investigationCost: null, appliesFromPeriod: null, generatorRunning: null } as ConfirmedControls,
  announcement: null as Announcement | null,
  lastRefresh: null as number | null,
  replayT: null as number | null,
  investigator: "",
  simAnalyst: null as SimAnalyst | null,
});

export function createDashboardStore() {
  let announceSeq = 0;
  return createStore<DashboardState>()((set) => ({
    ...initialData(),

    applyEvents: (batch) => set((s) => ({ feed: pushEvents(s.feed, batch) })),

    applyCaseMessage: (m) =>
      set((s) => {
        const prev = s.cases[m.case.case_id];
        const cases = upsertCase(s.cases, m.case);
        const ann = cases === s.cases ? null : announcementFor(prev, m.case, ++announceSeq);
        return {
          cases,
          queue: cases === s.cases ? s.queue : activeQueue(cases),
          backlogCount: m.backlog_count,
          announcement: ann ?? s.announcement,
        };
      }),

    upsertCase: (c) =>
      set((s) => {
        const cases = upsertCase(s.cases, c);
        return cases === s.cases ? {} : { cases, queue: activeQueue(cases) };
      }),

    replaceCases: (items, backlogCount) =>
      set(() => {
        const cases: Record<string, CaseSummary> = {};
        for (const c of items) cases[c.case_id] = c;
        return { cases, queue: activeQueue(cases), backlogCount, lastRefresh: Date.now() };
      }),

    setConnection: (connection) => set({ connection }),
    selectCase: (selectedCaseId) =>
      set((s) => (s.selectedCaseId === selectedCaseId ? { selectedCaseId } : { selectedCaseId, replayT: null })),
    setMetrics: (metrics) => set({ metrics }),
    setConfirmed: (patch) => set((s) => ({ confirmed: { ...s.confirmed, ...patch } })),
    setReplayT: (replayT) => set({ replayT }),
    setInvestigator: (investigator) => set({ investigator }),
    setSimAnalyst: (simAnalyst) => set({ simAnalyst }),
    reset: () => set(initialData()),
  }));
}

export type DashboardStore = ReturnType<typeof createDashboardStore>;

/** The app-wide store instance. Tests call `dashboard.getState().reset()` between cases. */
export const dashboard = createDashboardStore();

export function useDashboard<T>(selector: (s: DashboardState) => T): T {
  return useStore(dashboard, selector);
}
