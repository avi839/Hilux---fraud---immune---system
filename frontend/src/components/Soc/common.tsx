import { createContext, useContext, useEffect, type ReactNode } from "react";
import {
  SOC_ABSENT_MESSAGE,
  SOC_DISABLED_MESSAGE,
  type AuditVerifyDoc,
  type ConsoleDoc,
  type CoverageDoc,
  type PlaybookActionsDoc,
  type RulesDoc,
} from "../../api/soc";
import { checkInvestigator, type Checked } from "../Controls/validation";
import { Panel } from "../common/Panel";
import type { Msg, Resource } from "./useResource";

/** The investigator name typed once at the top of the SOC view; sent as ``by`` / ``author``. */
export const InvestigatorContext = createContext<string>("");

/** Documents the sections have loaded, shared with the KPI row so it never fetches twice. */
export interface SocFigures {
  console?: ConsoleDoc;
  pending?: PlaybookActionsDoc;
  rules?: RulesDoc;
  coverage?: CoverageDoc;
  auditTotal?: number;
  verify?: AuditVerifyDoc;
}

export const SocFiguresContext = createContext<(patch: SocFigures) => void>(() => undefined);

/** Report one loaded document (or figure) to the KPI row; nothing is reported until loaded. */
export function useReportFigure<K extends keyof SocFigures>(key: K, value: SocFigures[K] | null | undefined) {
  const report = useContext(SocFiguresContext);
  useEffect(() => {
    if (value !== null && value !== undefined) report({ [key]: value } as SocFigures);
  }, [report, key, value]);
}

export function useInvestigator(): () => Checked<string> {
  const name = useContext(InvestigatorContext);
  return () => checkInvestigator(name);
}

export function Message({ msg }: { msg: Msg }) {
  if (!msg) return null;
  return msg.kind === "error" ? (
    <p role="alert" className="msg msg--error">
      {msg.text}
    </p>
  ) : (
    <p role="status" className="msg msg--ok">
      {msg.text}
    </p>
  );
}

interface SectionProps<T> {
  title: string;
  area: string;
  /** One plain-language sentence saying what the section is for. */
  explain: string;
  resource: Resource<T> | null;
  /** Shown instead of the resource state when there is nothing to load (e.g. no Case selected). */
  idle?: ReactNode;
  children: (data: T) => ReactNode;
  /** Controls rendered whatever the resource state (e.g. a form that works on its own). */
  footer?: ReactNode;
}

/** A SOC panel: heading, one-line explanation, Refresh, and the resource's state. A missing
 *  route or disabled cyber fusion is a calm status message, not an error (Req 38.10). */
export function SocSection<T>({ title, area, explain, resource, idle, children, footer }: SectionProps<T>) {
  const r = resource;
  return (
    <Panel
      title={title}
      area={area}
      busy={r?.loading}
      actions={
        r ? (
          <button type="button" className="btn btn--small" onClick={r.reload} aria-label={`Refresh ${title.toLowerCase()}`}>
            Refresh
          </button>
        ) : null
      }
    >
      <p className="soc-explain">{explain}</p>
      {r === null ? (
        idle
      ) : r.status === "absent" ? (
        <p className="soc-unavailable" role="status" data-testid={`soc-absent-${area}`}>
          {SOC_ABSENT_MESSAGE}.
        </p>
      ) : r.status === "disabled" ? (
        <p className="soc-unavailable" role="status" data-testid={`soc-disabled-${area}`}>
          {SOC_DISABLED_MESSAGE}.
        </p>
      ) : (
        <>
          {r.status === "error" ? (
            <p className="msg msg--error" role="alert">
              {r.error}
              {r.data ? " Showing the last loaded data." : ""}
            </p>
          ) : null}
          {r.status === "loading" && !r.data ? (
            <p className="muted" role="status">
              Loading…
            </p>
          ) : null}
          {r.data ? children(r.data) : null}
        </>
      )}
      {footer}
    </Panel>
  );
}

export function pct(x: number | null | undefined, dp = 0): string {
  return typeof x === "number" && Number.isFinite(x) ? `${(x * 100).toFixed(dp)}%` : "n/a";
}

/** A duration in seconds as "1 h 05 m", "4 m 10 s" or "12 s"; negative values keep the sign. */
export function formatDuration(s: number | null | undefined): string {
  if (typeof s !== "number" || !Number.isFinite(s)) return "n/a";
  const neg = s < 0;
  let v = Math.round(Math.abs(s));
  const d = Math.floor(v / 86_400);
  v -= d * 86_400;
  const h = Math.floor(v / 3_600);
  v -= h * 3_600;
  const m = Math.floor(v / 60);
  const sec = v - m * 60;
  const pad = (n: number) => String(n).padStart(2, "0");
  const body = d ? `${d} d ${pad(h)} h` : h ? `${h} h ${pad(m)} m` : m ? `${m} m ${pad(sec)} s` : `${sec} s`;
  return neg ? `-${body}` : body;
}

/** Human names of the SLA_Timer keys. */
export const TIMER_LABEL: Record<string, string> = {
  time_to_acknowledge: "time to acknowledge",
  time_to_close: "time to close",
};
