import { useId } from "react";
import { socApi, type ResilienceDoc } from "../../api/soc";
import { Message, SocSection } from "./common";
import { useAction, useResource } from "./useResource";

function counters(doc: ResilienceDoc): string {
  const parts = Object.entries(doc.counters)
    .filter(([, v]) => typeof v === "number")
    .map(([k, v]) => `${k.replace(/_/g, " ")} ${v as number}`);
  return parts.join(", ") || "none";
}

function rates(doc: ResilienceDoc): string {
  const r = doc.rates;
  const pc = (x: number | undefined) => (typeof x === "number" ? `${Math.round(x * 100)}%` : "n/a");
  return `sensor failures ${pc(r.sensor_failure_rate)}, delays ${pc(r.delay_rate)}, bursts ${pc(r.burst_rate)}`;
}

/** Resilience_Mode (Req 37.5-37.8): one switch that injects sensor failures, delayed events and
 *  bursts with the server's default rates, plus what has been injected so far. */
export function ResilienceSection() {
  const r = useResource((sig) => socApi.resilience(sig), "resilience");
  const action = useAction();
  const id = useId();
  const enabled = r.data?.enabled ?? false;

  const toggle = () =>
    void action.run(async () => {
      const doc = await socApi.setResilience({ enabled: !enabled });
      r.reload();
      return doc.enabled ? "Resilience mode is on: failures are being injected." : "Resilience mode is off; held events were released.";
    });

  return (
    <SocSection
      title="Resilience mode"
      area="soc-resilience"
      explain="Deliberately breaks things (sensors fail, events arrive late or in bursts) to show HELIX keeps working when degraded."
      resource={r}
    >
      {(doc) => (
        <>
          <div className="soc-check">
            <input
              id={id}
              type="checkbox"
              role="switch"
              checked={doc.enabled}
              disabled={action.pending}
              onChange={toggle}
              data-testid="resilience-toggle"
            />
            <label htmlFor={id}>Resilience mode {doc.enabled ? "on" : "off"}</label>
          </div>
          <Message msg={action.msg} />
          <p className="small muted" data-testid="resilience-detail">
            Rates: {rates(doc)} · injected: {counters(doc)}
            {typeof doc.queue.length === "number" ? ` · queue ${doc.queue.length}` : ""}
            {typeof doc.queue.held === "number" ? `, held ${doc.queue.held}` : ""}
          </p>
        </>
      )}
    </SocSection>
  );
}
