import type { ReactNode } from "react";
import "./viewHeader.css";

interface ViewHeaderProps {
  /** id of the title heading, for aria-labelledby on the view's region. */
  id: string;
  /** Small label above the title (what kind of view this is). */
  eyebrow: string;
  title: string;
  /** One plain sentence saying what the view is for. */
  sub: ReactNode;
  /** Optional controls or status on the right of the header (a field, a timestamp). */
  aside?: ReactNode;
}

/** The common page header of the showcase views (SOC console, Red vs Blue, Trace, Live log,
 *  Workflow): eyebrow, display title and a one-line explanation, in the Home/Pricing style. */
export function ViewHeader({ id, eyebrow, title, sub, aside }: ViewHeaderProps) {
  return (
    <div className="hx-view-head">
      <div className="hx-view-head__text">
        <p className="hx-view-head__eyebrow">{eyebrow}</p>
        <h2 id={id} className="hx-view-head__title">
          {title}
        </h2>
        <p className="hx-view-head__sub">{sub}</p>
      </div>
      {aside ? <div className="hx-view-head__aside">{aside}</div> : null}
    </div>
  );
}
