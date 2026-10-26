import { useCallback, useId, useRef, useState } from "react";
import { CaseQueue } from "../CaseQueue/CaseQueue";
import { PanelBoundary } from "../common/ErrorBoundary";
import { DemoScenarioResult, useDemoScenario } from "../Controls/DemoScenario";
import { LiveFeed } from "../LiveFeed/LiveFeed";
import { LoopPanel, POLL_MS, useLoopData } from "../LoopPanel/LoopPanel";
import { CaseWorkspace } from "./CaseWorkspace";
import { ControlsDrawer } from "./ControlsDrawer";
import { KpiStrip } from "./KpiStrip";
import "./live.css";

/**
 * The live investigation dashboard (the Dashboard tab). DOM order is the reading and focus
 * order: KPI strip (with the Controls trigger and "Load demo scenario") → demo result line →
 * Case queue → Case workspace → Live feed and HELIX loop. Every view keeps its own error
 * boundary, so one failing view (e.g. WebGL) leaves the rest working. The loop metrics are
 * fetched once here and shared by the KPI strip and the HELIX loop card.
 */
export function LiveDashboard() {
  const loop = useLoopData(POLL_MS);
  const demo = useDemoScenario();
  const [controlsOpen, setControlsOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const drawerId = useId();

  const closeControls = useCallback(() => {
    setControlsOpen(false);
    trigger.current?.focus();
  }, []);

  return (
    <main id="main" className="lv" tabIndex={-1}>
      <KpiStrip
        loop={loop}
        demo={demo}
        controlsOpen={controlsOpen}
        drawerId={drawerId}
        triggerRef={trigger}
        onOpenControls={() => setControlsOpen(true)}
      />

      <div className="lv-demo" data-testid="demo-scenario" data-state={demo.run.status}>
        <DemoScenarioResult demo={demo} onDismiss={demo.dismiss} />
      </div>

      <div className="lv-main">
        <div className="lv-main__queue">
          <PanelBoundary title="Case queue" area="queue">
            <CaseQueue />
          </PanelBoundary>
        </div>
        <CaseWorkspace demo={demo} />
      </div>

      <div className="lv-bottom">
        <PanelBoundary title="Live feed" area="feed">
          <LiveFeed />
        </PanelBoundary>
        <PanelBoundary title="HELIX loop" area="loop">
          <LoopPanel data={loop} />
        </PanelBoundary>
      </div>

      {controlsOpen ? <ControlsDrawer id={drawerId} onClose={closeControls} /> : null}
    </main>
  );
}
