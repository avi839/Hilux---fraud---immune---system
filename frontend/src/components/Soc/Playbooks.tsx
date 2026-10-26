import { useId, useState } from "react";
import { socApi, type PlaybookActionDoc, type PlaybooksDoc } from "../../api/soc";
import { formatTime } from "../../format";
import { Message, SocSection, useInvestigator, useReportFigure } from "./common";
import { useAction, useResource } from "./useResource";

export const ACTIONS_POLL_MS = 10_000;

function PendingRow({ a, onDone }: { a: PlaybookActionDoc; onDone: () => void }) {
  const action = useAction();
  const who = useInvestigator();
  const [reason, setReason] = useState("");
  const reasonId = useId();
  const decide = (approve: boolean) => () => {
    const w = who();
    if (!w.ok) return action.setMsg({ kind: "error", text: w.message });
    void action.run(async () => {
      const r = approve ? await socApi.approveAction(a.action_id, w.value) : await socApi.rejectAction(a.action_id, w.value, reason.trim() || undefined);
      onDone();
      return `${r.action_id} is now ${r.status}.`;
    });
  };
  return (
    <li className="soc-card" data-testid={`action-${a.action_id}`}>
      <p className="soc-card__head">
        <span className="mono">{a.action_id}</span> <strong>{a.type}</strong> for case <span className="mono">{a.case_id}</span>
        {a.case_tier ? <span className="muted"> ({a.case_tier})</span> : null}
      </p>
      <p className="muted small">
        Targets: {a.targets.length ? a.targets.join(", ") : "none"}
        {a.playbook_id ? ` · playbook ${a.playbook_id} v${a.playbook_version ?? "?"}` : ""}
        {a.scheduled_at ? ` · scheduled ${formatTime(a.scheduled_at)}` : ""}
      </p>
      <div className="field">
        <label htmlFor={reasonId}>Reject reason (optional)</label>
        <input id={reasonId} type="text" maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)} />
      </div>
      <div className="buttons">
        <button type="button" className="btn" disabled={action.pending} onClick={decide(true)} aria-label={`Approve ${a.action_id}`}>
          Approve
        </button>
        <button type="button" className="btn btn--danger" disabled={action.pending} onClick={decide(false)} aria-label={`Reject ${a.action_id}`}>
          Reject
        </button>
      </div>
      <Message msg={action.msg} />
    </li>
  );
}

function DryRun({ doc, onChanged }: { doc: PlaybooksDoc; onChanged: () => void }) {
  const action = useAction();
  const who = useInvestigator();
  const toggle = () => {
    const w = who();
    if (!w.ok) return action.setMsg({ kind: "error", text: w.message });
    void action.run(async () => {
      const r = await socApi.setDryRun(!doc.dry_run, w.value);
      onChanged();
      return `Dry-run mode is ${r.dry_run ? "on" : "off"}.`;
    });
  };
  return (
    <div className="soc-sub">
      <p>
        Dry-run mode: <strong data-testid="dry-run">{doc.dry_run ? "on" : "off"}</strong>{" "}
        <span className="muted small">(on: actions are only recorded, nothing changes even in the simulation)</span>
      </p>
      <button type="button" className="btn btn--small" disabled={action.pending} onClick={toggle}>
        {doc.dry_run ? "Turn dry-run off" : "Turn dry-run on"}
      </button>
      <Message msg={action.msg} />
    </div>
  );
}

/** Response playbooks (Req 32): loaded playbooks, dry-run switch, and the approval queue of
 *  PENDING actions with approve / reject. Everything is simulated inside HELIX. */
export function PlaybooksSection() {
  const pbs = useResource((sig) => socApi.playbooks(sig), "playbooks");
  const pending = useResource((sig) => socApi.pendingActions(sig), "pending-actions", ACTIONS_POLL_MS);
  useReportFigure("pending", pending.data);
  const refresh = () => {
    pbs.reload();
    pending.reload();
  };
  return (
    <SocSection
      title="Response playbooks"
      area="soc-playbooks"
      explain="Standard responses (hold a payment, ask for re-authentication, freeze, notify) that run automatically; severe cases wait for a human to approve."
      resource={pbs}
      footer={
        pending.status === "ok" && pending.data ? (
          <div className="soc-sub">
            <h3 className="ev-section__title">Waiting for approval ({pending.data.count})</h3>
            {pending.data.actions.length === 0 ? (
              <p className="empty">No actions are waiting for approval.</p>
            ) : (
              <ul className="soc-cards" aria-label="Pending playbook actions">
                {pending.data.actions.map((a) => (
                  <PendingRow key={a.action_id} a={a} onDone={refresh} />
                ))}
              </ul>
            )}
          </div>
        ) : pending.status === "error" ? (
          <p className="msg msg--error" role="alert">
            Pending actions: {pending.error}
          </p>
        ) : null
      }
    >
      {(doc) => (
        <>
          <p className="soc-notice" data-testid="playbook-notice">
            {doc.notice}
          </p>
          <p className="small">
            Approval needed at tier <strong>{doc.approval_threshold}</strong> and above · actions so far:{" "}
            {Object.entries(doc.actions)
              .map(([k, n]) => `${k} ${n}`)
              .join(", ") || "none"}
          </p>
          <ul className="ev-list">
            {doc.playbooks.map((p) => (
              <li key={`${p.id}@${p.version}`}>
                <span className="mono">{p.id}</span> v{p.version} {p.title}{" "}
                <span className="muted small">
                  ({p.active ? "active" : p.enabled ? "enabled" : "disabled"}; when{" "}
                  {[p.trigger.min_tier ? `tier ≥ ${p.trigger.min_tier}` : null, p.trigger.tiers?.join("/"), p.trigger.techniques?.join("/")]
                    .filter(Boolean)
                    .join(` ${p.trigger.match ?? "ANY"} `) || "always"}
                  : {p.actions.map((a) => a.type).join(" → ")})
                </span>
              </li>
            ))}
          </ul>
          <DryRun doc={doc} onChanged={pbs.reload} />
        </>
      )}
    </SocSection>
  );
}
