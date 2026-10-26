import {
  createContext,
  useCallback,
  useContext,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import { ErrorBoundary } from "../common/ErrorBoundary";
import { LEADING_TABS, LIVE_TAB, SHOWCASE_TABS, type ShellTab } from "./tabs";

interface TabRef {
  id: string;
  label: string;
}

interface ShellState {
  active: string;
  select: (id: string) => void;
  /** Every tab in order (leading tabs, Dashboard, showcase tabs). */
  all: readonly TabRef[];
  /** The tabs with a View (everything except Dashboard), in order. */
  views: readonly ShellTab[];
  /** Tabs before Dashboard (their panels are rendered before the live panel). */
  leadingCount: number;
}

const ShellContext = createContext<ShellState>({
  active: LIVE_TAB.id,
  select: () => undefined,
  all: [LIVE_TAB],
  views: [],
  leadingCount: 0,
});

/** The selected tab and a way to switch it, e.g. for a call to action that opens another view. */
export function useShellTab(): ShellState {
  return useContext(ShellContext);
}

interface ShellProviderProps {
  /** Tabs after Dashboard; defaults to the registry in ./tabs. */
  tabs?: readonly ShellTab[];
  /** Tabs before Dashboard; defaults to the registry in ./tabs (Home). */
  leadingTabs?: readonly ShellTab[];
  /** Tab selected at first render; defaults to the first tab. */
  initialTab?: string;
  children: ReactNode;
}

/** Owns the selected tab. The tab list and the panels read it from context, so they can sit
 *  in different places of the layout (the tab list lives in the header). */
export function ShellProvider({ tabs = SHOWCASE_TABS, leadingTabs = LEADING_TABS, initialTab, children }: ShellProviderProps) {
  const all = useMemo<TabRef[]>(
    () => [...leadingTabs.map(({ id, label }) => ({ id, label })), { id: LIVE_TAB.id, label: LIVE_TAB.label }, ...tabs.map(({ id, label }) => ({ id, label }))],
    [tabs, leadingTabs],
  );
  const views = useMemo(() => [...leadingTabs, ...tabs], [tabs, leadingTabs]);
  const [active, setActive] = useState(() => (initialTab && all.some((t) => t.id === initialTab) ? initialTab : all[0]!.id));

  const select = useCallback(
    (id: string) => {
      if (all.some((t) => t.id === id)) setActive(id);
    },
    [all],
  );
  // A new view starts at the top of the page, not at the previous view's scroll position.
  const first = useRef(true);
  useLayoutEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    if (window.scrollY > 0) window.scrollTo(0, 0);
  }, [active]);
  const ctx = useMemo(() => ({ active, select, all, views, leadingCount: leadingTabs.length }), [active, select, all, views, leadingTabs.length]);
  return <ShellContext.Provider value={ctx}>{children}</ShellContext.Provider>;
}

/** WAI-ARIA tablist with automatic activation (Req 38.1, 38.11). Arrow Left/Right (wrapping),
 *  Home and End move the selection and focus; only the selected tab is in the tab order.
 *  The selected tab is marked by a light pill that slides between tabs; it is measured from
 *  the tab buttons, and until it has been measured the selected tab draws its own pill. */
export function ShellTabList() {
  const { active, select, all } = useShellTab();
  const refs = useRef(new Map<string, HTMLButtonElement>());
  const listRef = useRef<HTMLDivElement>(null);
  const [pill, setPill] = useState<{ x: number; w: number } | null>(null);

  useLayoutEffect(() => {
    let live = true;
    const measure = () => {
      if (!live) return;
      const el = refs.current.get(active);
      if (!el || el.offsetWidth === 0) {
        setPill(null);
        return;
      }
      setPill((p) => (p && p.x === el.offsetLeft && p.w === el.offsetWidth ? p : { x: el.offsetLeft, w: el.offsetWidth }));
      // Keep the selected tab in view when the row scrolls on narrow screens.
      const list = listRef.current;
      if (list && list.scrollWidth > list.clientWidth) {
        const left = el.offsetLeft - list.scrollLeft;
        if (left < 0 || left + el.offsetWidth > list.clientWidth) list.scrollLeft = el.offsetLeft - 16;
      }
    };
    measure();
    window.addEventListener("resize", measure);
    const ro = typeof ResizeObserver === "function" ? new ResizeObserver(measure) : null;
    if (ro && listRef.current) ro.observe(listRef.current);
    // Tab widths change once the bundled font has loaded.
    void document.fonts?.ready.then(measure);
    return () => {
      live = false;
      window.removeEventListener("resize", measure);
      ro?.disconnect();
    };
  }, [active, all]);

  const onKeyDown = (e: KeyboardEvent<HTMLButtonElement>) => {
    const i = all.findIndex((t) => t.id === active);
    const n = all.length;
    const next =
      e.key === "ArrowRight" ? (i + 1) % n : e.key === "ArrowLeft" ? (i - 1 + n) % n : e.key === "Home" ? 0 : e.key === "End" ? n - 1 : -1;
    if (next < 0) return;
    e.preventDefault();
    const id = all[next]!.id;
    select(id);
    refs.current.get(id)?.focus();
  };

  const pillStyle = pill ? ({ "--pill-x": `${pill.x}px`, "--pill-w": `${pill.w}px` } as CSSProperties) : undefined;
  return (
    <div className={`hx-tabs${pill ? " hx-tabs--measured" : ""}`} style={pillStyle}>
      <div ref={listRef} className="tabs" role="tablist" aria-label="Dashboard views">
        <span className="hx-tabs__pill" aria-hidden="true" />
        {all.map((t) => {
          const selected = t.id === active;
          return (
            <button
              key={t.id}
              ref={(el) => {
                if (el) refs.current.set(t.id, el);
                else refs.current.delete(t.id);
              }}
              type="button"
              role="tab"
              id={`tab-${t.id}`}
              aria-selected={selected}
              aria-controls={`panel-${t.id}`}
              tabIndex={selected ? 0 : -1}
              className={`tab${selected ? " tab--active" : ""}`}
              onClick={() => select(t.id)}
              onKeyDown={onKeyDown}
            >
              {t.label}
            </button>
          );
        })}
      </div>
    </div>
  );
}

function ViewPanel({ tab }: { tab: ShellTab }) {
  const { active } = useShellTab();
  const { id, label, View } = tab;
  return (
    <div id={`panel-${id}`} role="tabpanel" aria-labelledby={`tab-${id}`} hidden={active !== id} className={`tabpanel tabpanel--full tabpanel--${id}`}>
      {active === id ? (
        <ErrorBoundary
          fallback={(error, reset) => (
            <div role="alert" className="msg msg--error" data-testid={`tab-error-${id}`}>
              <p className="msg__text">
                The {label} view failed: {error.message}. The other tabs keep working.
              </p>
              <button type="button" className="btn" onClick={reset}>
                Retry
              </button>
            </div>
          )}
        >
          <View />
        </ErrorBoundary>
      ) : null}
    </div>
  );
}

/** The tab panels. The Dashboard panel (``children``) stays mounted while hidden; every other
 *  view is mounted only while selected, and a view that fails to render shows an error in
 *  its own panel while the other tabs keep working (Req 38.10). */
export function ShellPanels({ children }: { children: ReactNode }) {
  const { active, views, leadingCount } = useShellTab();
  return (
    <>
      {views.slice(0, leadingCount).map((t) => (
        <ViewPanel key={t.id} tab={t} />
      ))}
      <div
        id={`panel-${LIVE_TAB.id}`}
        role="tabpanel"
        aria-labelledby={`tab-${LIVE_TAB.id}`}
        hidden={active !== LIVE_TAB.id}
        className={`tabpanel tabpanel--${LIVE_TAB.id}`}
      >
        {children}
      </div>
      {views.slice(leadingCount).map((t) => (
        <ViewPanel key={t.id} tab={t} />
      ))}
    </>
  );
}

interface TabbedDashboardProps extends Omit<ShellProviderProps, "children"> {
  /** The Dashboard tab's content (the investigation grid). It stays mounted while hidden. */
  children: ReactNode;
}

/** Tab list and panels in one block: the shell without the app header around it. */
export function TabbedDashboard({ children, ...provider }: TabbedDashboardProps) {
  return (
    <ShellProvider {...provider}>
      <ShellTabList />
      <ShellPanels>{children}</ShellPanels>
    </ShellProvider>
  );
}
