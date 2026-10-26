// HELIX Pricing: illustrative organisation plans with a monthly/annual billing toggle, a
// comparison table and a short FAQ. There is no billing behind it; the CTAs only switch
// dashboard tabs. Plan scope follows README "Using HELIX in an organisation"; anything a
// large bank would need that does not exist yet is marked "Roadmap".

import "@fontsource-variable/sora";
import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { useShellTab } from "../Shell/TabbedDashboard";
import "./pricing.css";

export type Billing = "monthly" | "annual";

/** Annual billing discount (shown as "Annual: save 20%"). */
export const ANNUAL_DISCOUNT = 0.2;

/** Per-month price when billed annually: 20% off, rounded to the nearest 10 USD. */
export function annualPerMonth(monthly: number): number {
  return Math.round((monthly * (1 - ANNUAL_DISCOUNT)) / 10) * 10;
}

export function usd(n: number): string {
  return `$${Math.round(n).toLocaleString("en-US")}`;
}

interface PlanFeature {
  text: string;
  /** Not built yet: shown with a "Roadmap" label. */
  roadmap?: boolean;
}

export interface Plan {
  id: "pilot" | "starter" | "growth" | "enterprise";
  name: string;
  audience: string;
  /** Monthly price in USD; null for a custom quote. */
  monthly: number | null;
  features: PlanFeature[];
  cta: { label: string; tab: "live" | "workflow" };
  popular?: boolean;
}

export const PLANS: Plan[] = [
  {
    id: "pilot",
    name: "Pilot",
    audience: "For evaluation and hackathons",
    monthly: 0,
    features: [
      { text: "Full six-stage core loop" },
      { text: "1 investigator" },
      { text: "Synthetic data and AMLworld replay" },
      { text: "Community support" },
    ],
    cta: { label: "Start pilot", tab: "live" },
  },
  {
    id: "starter",
    name: "Starter",
    audience: "For credit unions and small fintechs",
    monthly: 1490,
    features: [
      { text: "Up to 50k accounts" },
      { text: "5 investigators" },
      { text: "Core loop and Red vs Blue" },
      { text: "Email support" },
    ],
    cta: { label: "Book a demo", tab: "workflow" },
  },
  {
    id: "growth",
    name: "Growth",
    audience: "For mid-size banks and PSPs",
    monthly: 4990,
    popular: true,
    features: [
      { text: "Up to 1M accounts" },
      { text: "25 investigators" },
      { text: "Everything in Starter" },
      { text: "SOC layer: kill chain, playbooks, canaries, threat intel, audit ledger" },
      { text: "CSV onboarding and warm-up" },
      { text: "Priority support" },
    ],
    cta: { label: "Book a demo", tab: "workflow" },
  },
  {
    id: "enterprise",
    name: "Enterprise",
    audience: "For large banks",
    monthly: null,
    features: [
      { text: "Unlimited accounts and investigators" },
      { text: "Everything in Growth" },
      { text: "Horizontal scaling", roadmap: true },
      { text: "SSO and role-based access", roadmap: true },
      { text: "Model governance", roadmap: true },
      { text: "Core-banking integration", roadmap: true },
      { text: "Dedicated support" },
    ],
    cta: { label: "Contact sales", tab: "workflow" },
  },
];

/** Comparison cell: true = included, false = not included, string = the value. */
type Cell = boolean | string;

export const COMPARISON: [string, [Cell, Cell, Cell, Cell]][] = [
  ["Core loop (SENSE to LEARN)", [true, true, true, true]],
  ["Sensors", ["6 core", "6 core", "7, with SOC", "7, with SOC"]],
  ["Red vs Blue", [false, true, true, true]],
  ["SOC layer", [false, false, true, true]],
  ["Dataset / CSV onboarding", ["Synthetic + AMLworld", "Synthetic + AMLworld", "CSV mapping + warm-up", "CSV mapping + warm-up"]],
  ["Investigators", ["1", "5", "25", "Unlimited"]],
  ["Accounts", ["Synthetic only", "Up to 50k", "Up to 1M", "Unlimited"]],
  ["Support", ["Community", "Email", "Priority", "Dedicated"]],
];

const FAQ: [string, string][] = [
  [
    "What counts as an account?",
    "Any customer account HELIX keeps a behavioural profile (Account DNA) for, that is, any account that sends or receives a transaction in the scored stream. Accounts with no events do not count.",
  ],
  [
    "Can we run it on-premises?",
    "Yes, HELIX runs as a single local process today, with one DuckDB file for persistence. It needs no cloud service.",
  ],
  ["Is my data sent anywhere?", "No external network calls. Scoring, storage and this dashboard all run on your own machine."],
  [
    "Can we switch plans?",
    "Yes. Plans differ only in limits, modules and support, so moving up or down changes no data or configuration.",
  ],
];

const BILLING_OPTIONS: { id: Billing; label: string }[] = [
  { id: "monthly", label: "Monthly" },
  { id: "annual", label: "Annual" },
];

const TWEEN_MS = 450;

function canAnimate(): boolean {
  return (
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    typeof window.requestAnimationFrame === "function" &&
    window.matchMedia("(prefers-reduced-motion: no-preference)").matches
  );
}

/** Eases the displayed number to ``value``. Without motion support or with a reduced-motion
 *  preference it returns ``value`` directly. */
function useTweened(value: number): number {
  const [shown, setShown] = useState(value);
  const current = useRef(value);
  const animate = canAnimate();
  useEffect(() => {
    const from = current.current;
    if (!animate || from === value) {
      current.current = value;
      setShown(value);
      return;
    }
    const t0 = performance.now();
    let raf = 0;
    const step = (t: number) => {
      const p = Math.min(1, (t - t0) / TWEEN_MS);
      const eased = 1 - Math.pow(1 - p, 3);
      current.current = from + (value - from) * eased;
      setShown(current.current);
      if (p < 1) raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [value, animate]);
  return animate ? shown : value;
}

function CheckIcon({ roadmap = false }: { roadmap?: boolean }) {
  return (
    <svg className={`pricing-icon${roadmap ? " pricing-icon--roadmap" : ""}`} viewBox="0 0 16 16" fill="none" aria-hidden="true" focusable="false">
      {roadmap ? (
        <circle cx="8" cy="8" r="6.25" stroke="currentColor" strokeWidth="1.3" strokeDasharray="2.2 2" />
      ) : (
        <>
          <circle cx="8" cy="8" r="7" fill="currentColor" opacity="0.14" />
          <path d="M4.8 8.2 7 10.3l4.2-4.6" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
        </>
      )}
    </svg>
  );
}

function Rule() {
  return (
    <div className="pricing-rule" aria-hidden="true">
      <i className="pricing-rule__dot pricing-rule__dot--l" />
      <i className="pricing-rule__dot pricing-rule__dot--r" />
    </div>
  );
}

/** Monthly | Annual as a radio group: arrow keys move and select, only the checked option
 *  is in the tab order, and a decorative pill slides under the checked option. */
function BillingToggle({ value, onChange }: { value: Billing; onChange: (b: Billing) => void }) {
  const refs = useRef(new Map<Billing, HTMLButtonElement>());
  const onKeyDown = (e: KeyboardEvent<HTMLButtonElement>) => {
    let next: Billing | null = null;
    if (e.key === "ArrowRight" || e.key === "ArrowDown" || e.key === "ArrowLeft" || e.key === "ArrowUp") {
      next = value === "monthly" ? "annual" : "monthly";
    } else if (e.key === "Home") next = "monthly";
    else if (e.key === "End") next = "annual";
    if (!next) return;
    e.preventDefault();
    onChange(next);
    refs.current.get(next)?.focus();
  };
  return (
    <div className="pricing-billing">
      <div className="pricing-toggle" role="radiogroup" aria-label="Billing period" data-value={value}>
        <span className="pricing-toggle__pill" aria-hidden="true" />
        {BILLING_OPTIONS.map((o) => {
          const checked = o.id === value;
          return (
            <button
              key={o.id}
              ref={(el) => {
                if (el) refs.current.set(o.id, el);
                else refs.current.delete(o.id);
              }}
              type="button"
              role="radio"
              aria-checked={checked}
              tabIndex={checked ? 0 : -1}
              className="pricing-toggle__opt"
              onClick={() => onChange(o.id)}
              onKeyDown={onKeyDown}
            >
              {o.label}
            </button>
          );
        })}
      </div>
      <span className="hx-chip hx-chip--accent pricing-save">Annual: save 20%</span>
    </div>
  );
}

function Price({ plan, billing }: { plan: Plan; billing: Billing }) {
  const target = plan.monthly === null ? 0 : billing === "annual" ? annualPerMonth(plan.monthly) : plan.monthly;
  const shown = useTweened(target);
  if (plan.monthly === null) {
    return (
      <>
        <p className="pricing-price" data-testid={`price-${plan.id}`}>
          <span className="pricing-price__amount">Custom</span>
        </p>
        <p className="pricing-price__note">Priced to your volume</p>
      </>
    );
  }
  const note = plan.monthly === 0 ? "Free for evaluation" : billing === "annual" ? `${usd(target * 12)} billed annually` : "Billed monthly";
  return (
    <>
      <p className="pricing-price" data-testid={`price-${plan.id}`}>
        <span className="pricing-price__amount">{usd(shown)}</span>
        <span className="pricing-price__per">/ month</span>
      </p>
      <p className="pricing-price__note" data-testid={`price-note-${plan.id}`}>
        {note}
      </p>
    </>
  );
}

function PlanCard({ plan, billing }: { plan: Plan; billing: Billing }) {
  const { select } = useShellTab();
  return (
    <li className={`hx-card pricing-card${plan.popular ? " pricing-card--popular" : ""}`} data-testid={`plan-${plan.id}`}>
      <div className="pricing-card__head">
        <h3 className="pricing-card__name">{plan.name}</h3>
        {plan.popular ? <span className="hx-chip pricing-popular">Most popular</span> : null}
      </div>
      <p className="pricing-card__for">{plan.audience}</p>
      <Price plan={plan} billing={billing} />
      <button
        type="button"
        className={`hx-pill pricing-cta${plan.popular ? " hx-pill--primary" : ""}`}
        onClick={() => select(plan.cta.tab)}
      >
        {plan.cta.label}
        <span className="sr-only"> for {plan.name}</span>
      </button>
      <ul className="pricing-features">
        {plan.features.map((f) => (
          <li key={f.text}>
            <CheckIcon roadmap={f.roadmap} />
            <span>{f.text}</span>
            {f.roadmap ? (
              <>
                {" "}
                <span className="hx-chip pricing-roadmap">Roadmap</span>
              </>
            ) : null}
          </li>
        ))}
      </ul>
    </li>
  );
}

function CellValue({ value }: { value: Cell }) {
  if (value === true) {
    return (
      <span className="pricing-included">
        <CheckIcon />
        Included
      </span>
    );
  }
  if (value === false) {
    return (
      <span className="pricing-none">
        <span aria-hidden="true">—</span>
        <span className="sr-only">Not included</span>
      </span>
    );
  }
  return <>{value}</>;
}

function Comparison() {
  return (
    <section className="pricing-section">
      <p className="pricing-section__eyebrow">Compare</p>
      <div className="hx-card pricing-table-wrap" role="region" aria-labelledby="pricing-compare" tabIndex={0}>
        <table className="pricing-table">
          <caption id="pricing-compare">Compare plans</caption>
          <thead>
            <tr>
              <th scope="col">Feature</th>
              {PLANS.map((p) => (
                <th key={p.id} scope="col" className={p.popular ? "pricing-col--popular" : undefined}>
                  {p.name}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {COMPARISON.map(([label, cells]) => (
              <tr key={label}>
                <th scope="row">{label}</th>
                {cells.map((c, i) => (
                  <td key={PLANS[i]!.id} className={PLANS[i]!.popular ? "pricing-col--popular" : undefined}>
                    <CellValue value={c} />
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function Faq() {
  return (
    <section className="pricing-section" aria-labelledby="pricing-faq">
      <p className="pricing-section__eyebrow">FAQ</p>
      <h3 id="pricing-faq" className="pricing-faq-title">
        Questions teams ask
      </h3>
      <div className="pricing-faq">
        {FAQ.map(([q, a]) => (
          <details key={q} className="pricing-faq__item">
            <summary>{q}</summary>
            <p>{a}</p>
          </details>
        ))}
      </div>
    </section>
  );
}

export function PricingPage() {
  const [billing, setBilling] = useState<Billing>("monthly");
  return (
    <div className="pricing" role="region" aria-labelledby="pricing-title" data-testid="pricing">
      <div className="pricing-hero">
        <div className="hx-spotlight pricing-spot" aria-hidden="true" />
        <p className="hx-eyebrow pricing-eyebrow">Pricing</p>
        <h2 id="pricing-title" className="hx-section__title pricing-title">
          Plans for every fraud team
        </h2>
        <p className="hx-sub pricing-sub">Start with a free pilot on synthetic data, then scale to your accounts, investigators and SOC.</p>
        <BillingToggle value={billing} onChange={setBilling} />
        <p className="sr-only" role="status">
          {billing === "annual" ? "Showing annual billing: prices per month, billed annually." : "Showing monthly billing."}
        </p>
      </div>
      <Rule />
      <ul className="pricing-plans">
        {PLANS.map((p) => (
          <PlanCard key={p.id} plan={p} billing={billing} />
        ))}
      </ul>
      <Comparison />
      <Faq />
      <p className="pricing-honesty">Illustrative pricing for this hackathon prototype. HELIX is not a commercial product yet; no billing exists.</p>
    </div>
  );
}
