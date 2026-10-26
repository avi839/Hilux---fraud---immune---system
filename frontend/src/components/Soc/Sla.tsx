import { useId, useState, type FormEvent } from "react";
import { socApi, type ConsoleCaseDoc, type ConsoleDoc } from "../../api/soc";
import { formatTime } from "../../format";
import { Message, SocSection, TIMER_LABEL, formatDuration, useInvestigator, useReportFigure } from "./common";
import { useAction, useResource } from "./useResource";

export const CONSOLE_POLL_MS = 10_000;
export const BREACHED_TEXT = "SLA BREACHED";
const NOTE_MAX = 2_000;

function timerCell(c: ConsoleCaseDoc, key: string) {
  const t = c.timers[key];
  if (!t) return "n/a";
  const state = t.breached ? `over by ${formatDuration(-t.remaining_s)}` : t.running ? `${formatDuration(t.remaining_s)} left` : "stopped";
  return (
    <>
      {formatDuration(t.elapsed_s)} of {formatDuration(t.target_s)}
      <div className="muted small">{state}</div>
    </>
  );
}

function SlaTable({ doc }: { doc: ConsoleDoc }) {
  if (doc.cases.length === 0) return <p className="empty">No open cases.</p>;
  return (
    <div className="scroll scroll--short" tabIndex={0} role="group" aria-label="SLA timers table">
      <table className="table table--compact">
        <caption className="sr-only">SLA timers of the open cases, breached first</caption>
        <thead>
          <tr>
            <th scope="col">Case</th>
            <th scope="col">Tier</th>
            <th scope="col">Assignee</th>
            <th scope="col">Age</th>
            <th scope="col">{TIMER_LABEL.time_to_acknowledge}</th>
            <th scope="col">{TIMER_LABEL.time_to_close}</th>
            <th scope="col">SLA</th>
          </tr>
        </thead>
        <tbody>
          {doc.cases.map((c) => (
            <tr key={c.case_id} className={c.breached ? "row--breach" : undefined} data-testid={`sla-${c.case_id}`}>
              <th scope="row" className="mono">
                {c.case_id}
              </th>
              <td>
                <span className={`tier tier--${c.tier.toLowerCase()}`}>{c.tier}</span>
              </td>
              <td>{c.assignee ?? "unassigned"}</td>
              <td className="mono">{formatDuration(c.age_s)}</td>
              <td className="mono">{timerCell(c, "time_to_acknowledge")}</td>
              <td className="mono">{timerCell(c, "time_to_close")}</td>
              <td>
                {c.breached ? (
                  <strong className="text--danger" data-testid={`sla-breached-${c.case_id}`}>
                    {c.label ?? BREACHED_TEXT}
                    {c.breached_timers.length ? (
                      <span className="muted small"> ({c.breached_timers.map((k) => TIMER_LABEL[k] ?? k).join(", ")})</span>
                    ) : null}
                  </strong>
                ) : (
                  "within SLA"
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function HandoffForm({ onAdded }: { onAdded: () => void }) {
  const [text, setText] = useState("");
  const action = useAction();
  const who = useInvestigator();
  const id = useId();
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const w = who();
    if (!w.ok) return action.setMsg({ kind: "error", text: w.message });
    if (!text.trim()) return action.setMsg({ kind: "error", text: "Write the handoff note first." });
    void action.run(async () => {
      const note = await socApi.addHandoffNote(w.value, text.trim());
      setText("");
      onAdded();
      return `Handoff note ${note.note_id} saved by ${note.author}.`;
    });
  };
  return (
    <form className="soc-sub" onSubmit={submit} noValidate>
      <div className="field">
        <label htmlFor={id}>New shift handoff note</label>
        <textarea id={id} rows={3} maxLength={NOTE_MAX} value={text} onChange={(e) => setText(e.target.value)} />
      </div>
      <button type="submit" className="btn btn--small" disabled={action.pending}>
        Add note
      </button>
      <Message msg={action.msg} />
    </form>
  );
}

/** SOC_Console (Req 36, 38.2): SLA_Timers per open Case ("SLA BREACHED" for every breached
 *  one, in text, not colour only), workload, MTTD / MTTR and the shift handoff notes. */
export function SlaSection() {
  const r = useResource((sig) => socApi.console(sig), "soc-console", CONSOLE_POLL_MS);
  useReportFigure("console", r.data);
  return (
    <SocSection
      title="SLA timers and handoff"
      area="soc-sla"
      explain="How long each open case has waited against its severity target, who is working on what, and notes for the next shift."
      resource={r}
      footer={r.status === "ok" || r.data ? <HandoffForm onAdded={r.reload} /> : null}
    >
      {(doc) => (
        <>
          <p data-testid="sla-summary">
            {doc.open_cases} open case{doc.open_cases === 1 ? "" : "s"} ·{" "}
            <strong className={doc.breached_cases ? "text--danger" : undefined}>
              {doc.breached_cases} {BREACHED_TEXT.toLowerCase()}
            </strong>{" "}
            · MTTD {formatDuration(doc.mttd.mean_s)} ({doc.mttd.cases}) · MTTR {formatDuration(doc.mttr.mean_s)} ({doc.mttr.cases})
          </p>
          <SlaTable doc={doc} />
          {doc.workload.length ? (
            <p className="small">
              In review:{" "}
              {doc.workload.map((w) => `${w.investigator} ${w.in_review}`).join(", ")}
            </p>
          ) : null}
          <div className="soc-handoff" data-testid="handoff-notes">
            <h3 className="soc-subsection">Shift handoff notes</h3>
            {doc.handoff_notes.length === 0 ? (
              <p className="empty">No handoff notes yet.</p>
            ) : (
              <ul className="ev-list">
                {doc.handoff_notes.map((n) => (
                  <li key={n.note_id}>
                    <span className="mono small muted">{formatTime(n.ts)}</span> <strong>{n.author}</strong>: {n.text}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </>
      )}
    </SocSection>
  );
}
