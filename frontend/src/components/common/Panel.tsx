import { useId, type KeyboardEvent, type ReactNode } from "react";

interface PanelProps {
  title: string;
  area: string;
  actions?: ReactNode;
  /** The body scrolls on its own; make it a keyboard tab stop so it can be scrolled
   *  without a pointer (it may contain no other focusable element). */
  scrollable?: boolean;
  /** Set while the panel's content is being (re)loaded. */
  busy?: boolean;
  /** Key handler on the section root (keys bubble from any focused descendant). */
  onKeyDown?: (e: KeyboardEvent<HTMLElement>) => void;
  /** Make the section itself a programmatic focus target (tabIndex -1), so a click on its
   *  non-interactive content puts focus inside it, e.g. for panel-scoped shortcuts. */
  focusTarget?: boolean;
  children: ReactNode;
}

/** A titled grid region; the section is labelled by its heading for landmark navigation. */
export function Panel({ title, area, actions, scrollable, busy, onKeyDown, focusTarget, children }: PanelProps) {
  const id = useId();
  return (
    <section
      className={`panel panel--${area}`}
      aria-labelledby={id}
      aria-busy={busy || undefined}
      onKeyDown={onKeyDown}
      tabIndex={focusTarget ? -1 : undefined}
    >
      <header className="panel__header">
        <h2 id={id} className="panel__title">
          {title}
        </h2>
        {actions ? <div className="panel__actions">{actions}</div> : null}
      </header>
      {scrollable ? (
        <div className="panel__body panel__body--scroll" tabIndex={0} role="group" aria-labelledby={id}>
          {children}
        </div>
      ) : (
        <div className="panel__body">{children}</div>
      )}
    </section>
  );
}

/** Stand-in for a view delivered by a later task. */
export function Placeholder({ title, area, note }: { title: string; area: string; note: string }) {
  return (
    <Panel title={title} area={area}>
      <p className="placeholder">{note}</p>
    </Panel>
  );
}
