import type { ComponentType } from "react";
import { Arena } from "../Arena/Arena";
import { BenchmarkPage } from "../Benchmark/BenchmarkPage";
import { HomePage } from "../Home/HomePage";
import { LiveLog } from "../LiveLog/LiveLog";
import { PricingPage } from "../Pricing/PricingPage";
import { SocConsole } from "../Soc/SocConsole";
import { TransactionTrace } from "../TransactionTrace/TransactionTrace";
import { WorkflowView } from "../Workflow/WorkflowView";

/** One view tab of the dashboard shell (Req 38.1). ``id`` is used in element ids, so keep
 *  it to [a-z0-9-]. The view is mounted only while its tab is selected. */
export interface ShellTab {
  id: string;
  label: string;
  View: ComponentType;
}

/** The live investigation grid, passed to the shell as children. Unlike the other tabs it
 *  stays mounted while hidden, because it holds the WebSocket-driven state. */
export const LIVE_TAB = { id: "live", label: "Dashboard" } as const;

/** Tabs before the Dashboard tab. The app opens on the first tab. */
export const LEADING_TABS: ShellTab[] = [{ id: "home", label: "Home", View: HomePage }];

/** Showcase tabs after the Dashboard tab, in order. Adding a view is one line here. */
export const SHOWCASE_TABS: ShellTab[] = [
  { id: "workflow", label: "Workflow", View: WorkflowView },
  { id: "soc", label: "SOC console", View: SocConsole },
  { id: "arena", label: "Red vs Blue", View: Arena },
  { id: "benchmark", label: "Benchmark", View: BenchmarkPage },
  { id: "trace", label: "Trace", View: TransactionTrace },
  { id: "log", label: "Live log", View: LiveLog },
  { id: "pricing", label: "Pricing", View: PricingPage },
];
