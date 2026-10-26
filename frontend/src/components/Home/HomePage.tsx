// HELIX Home: the landing page of the dashboard. Calm hero on a hairline grid, fanned
// miniatures of real screens, a live strip from the store, the six-stage loop, a feature
// bento and the adoption path. All numbers are the README walkthrough's real values or come
// live from the store; none are invented.

import "@fontsource-variable/sora";
import { useRef, type ReactNode } from "react";
import { useShellTab } from "../Shell/TabbedDashboard";
import { HeroCards } from "./HeroCards";
import { HelixLogo } from "../Brand/Logo";
import { Icon, type IconName } from "./icons";
import { LiveStrip } from "./LiveStrip";
import { Reveal } from "./motion";
import { WorkflowPreview } from "./WorkflowPreview";
import "./home.css";

/** Test counts as recorded in README.md "Tests" (last full run). */
export const BUILT = {
  stages: 6,
  sensorsWithSoc: 7,
  properties: 90,
  backendTests: 1057,
  frontendTests: 371,
} as const;

type TabId = "live" | "workflow" | "soc" | "arena" | "trace" | "log" | "pricing";

const TAB_LABEL: Record<TabId, string> = {
  live: "Dashboard",
  workflow: "Workflow",
  soc: "SOC console",
  arena: "Red vs Blue",
  trace: "Trace",
  log: "Live log",
  pricing: "Pricing",
};

export const STAGES: { name: string; icon: IconName; text: string; example: string }[] = [
  { name: "SENSE", icon: "sense", text: "Checks every event: valid, new and on time.", example: "T009 rejected: amount -20.00" },
  {
    name: "PERCEIVE",
    icon: "perceive",
    text: "Six sensors vote fraud, legit or can’t tell.",
    example: "T007 fused belief 0.949",
  },
  {
    name: "REMEMBER",
    icon: "remember",
    text: "Compares the event with the account’s own habits.",
    example: "Mean 37.69 EUR → ABRUPT_BREAK",
  },
  { name: "REASON", icon: "reason", text: "Graph motifs catch structuring and mule chains.", example: "Carl → Dave → Erin → Finn" },
  { name: "ACT", icon: "act", text: "Opens a Case, ranks it and explains why.", example: "Risk 0.973 → CRITICAL Case C-1" },
  { name: "LEARN", icon: "learn", text: "Each label re-weights the sensors by 0.05.", example: "False positive: 1.0 → 0.95" },
];

interface Feature {
  title: string;
  icon: IconName;
  lines: [string, string];
  tab?: TabId;
  size: "xl" | "lg" | "md" | "sm" | "xs";
  visual?: ReactNode;
}

function FusionVisual() {
  const rows: [string, number, number, number][] = [
    ["Sensor A", 0.6, 0.1, 0.3],
    ["Sensor B", 0.5, 0.2, 0.3],
    ["Fused", 0.759, 0.133, 0.108],
  ];
  return (
    <div className="home-fv" aria-hidden="true">
      {rows.map(([name, f, l, u]) => (
        <div key={name} className={`home-fv__row${name === "Fused" ? " home-fv__row--fused" : ""}`}>
          <span className="home-fv__name">{name}</span>
          <span className="home-fv__bar">
            <span className="home-fv__f" style={{ flexGrow: f }} />
            <span className="home-fv__l" style={{ flexGrow: l }} />
            <span className="home-fv__u" style={{ flexGrow: u }} />
          </span>
          <span className="home-fv__nums">
            {f.toFixed(f === 0.759 ? 3 : 2)} / {l.toFixed(l === 0.133 ? 3 : 2)} / {u.toFixed(u === 0.108 ? 3 : 2)}
          </span>
        </div>
      ))}
      <p className="home-fv__key">fraud / legit / can&apos;t tell</p>
    </div>
  );
}

export const FEATURES: Feature[] = [
  {
    title: "Evidence fusion that keeps “can’t tell”",
    icon: "fusion",
    lines: [
      "Dempster-Shafer fusion keeps fraud, legit and unknown apart, so two moderate votes become a stronger one.",
      "An account with no history scores risk 0.5, not “safe”.",
    ],
    tab: "trace",
    size: "xl",
    visual: <FusionVisual />,
  },
  {
    title: "Account DNA and change points",
    icon: "dna",
    lines: ["Decayed habits per account: amounts, hours, places, payees.", "A new job becomes the new normal; a takeover stays an abrupt break."],
    tab: "trace",
    size: "md",
  },
  {
    title: "Temporal graph motifs",
    icon: "graph",
    lines: ["A 7-day who-paid-whom graph finds cycles, fan-out and structuring.", "Mule chains that no single payment shows."],
    tab: "live",
    size: "md",
  },
  {
    title: "Ranked cases under an alert budget",
    icon: "ranked",
    lines: ["Cases are ranked by expected loss minus investigation cost.", "Up to 20 per hour reach the queue; the rest wait in the backlog."],
    tab: "live",
    size: "sm",
  },
  {
    title: "Learns from every investigator label",
    icon: "loop",
    lines: ["Each label moves the sensors that blamed the account, within bounds.", "Every weight change is recorded and can be rolled back."],
    tab: "live",
    size: "sm",
  },
  {
    title: "Red vs Blue: an attacker and an immune system",
    icon: "arena",
    lines: ["The attacker mutates the campaigns that got through.", "The immune system grows antibodies from its misses, tested against normal traffic."],
    tab: "arena",
    size: "sm",
  },
  {
    title: "SOC fusion: logins, canaries, playbooks",
    icon: "soc",
    lines: ["Login attacks and canary hits join the fraud Case with a kill chain.", "Playbooks run, and HIGH+ actions wait for approval."],
    tab: "soc",
    size: "lg",
  },
  {
    title: "Tamper-evident audit ledger",
    icon: "ledger",
    lines: ["Labels, approvals and weight changes go into a SHA-256 hash chain.", "Verify checks the whole chain end to end."],
    tab: "soc",
    size: "xs",
  },
  {
    title: "Bring your own data: CSV mapping and warm-up",
    icon: "data",
    lines: ["Map a CSV export with one Column_Mapping YAML.", "Warm up on 30–90 days of history, then go live."],
    tab: "live",
    size: "sm",
  },
];

const PHASES = [
  { name: "Shadow", text: "Score a copy of production traffic with playbooks in dry-run. Nobody acts; compare Cases with the current system." },
  { name: "Assisted", text: "Investigators work HELIX Cases next to the existing queue. Their labels train the sensors; actions stay behind approval." },
  { name: "Active", text: "HELIX’s queue becomes primary for covered products. Low-tier actions run automatically; HIGH and CRITICAL keep the gate." },
];

function Cta() {
  const { select } = useShellTab();
  return (
    <div className="home-cta">
      <button type="button" className="hx-pill hx-pill--primary home-pill home-pill--primary" onClick={() => select("live")}>
        Open dashboard
        <span aria-hidden="true" className="home-pill__arrow">
          →
        </span>
      </button>
      <button type="button" className="hx-pill home-pill" onClick={() => select("workflow")}>
        Watch it work
      </button>
    </div>
  );
}

function SectionHead({ id, eyebrow, title, lede }: { id: string; eyebrow: string; title: string; lede?: string }) {
  return (
    <Reveal className="home-head">
      <p className="hx-eyebrow home-eyebrow">{eyebrow}</p>
      <h2 id={id} className="hx-section__title home-h2">
        {title}
      </h2>
      {lede ? <p className="home-lede">{lede}</p> : null}
    </Reveal>
  );
}

/** Full-width hairline with dots where it crosses the page's two vertical grid lines. */
function Rule() {
  return (
    <div className="home-rule" aria-hidden="true">
      <i className="home-rule__dot home-rule__dot--l" />
      <i className="home-rule__dot home-rule__dot--r" />
    </div>
  );
}

function Hero() {
  const host = useRef<HTMLElement | null>(null);
  return (
    <section className="home-hero" ref={host} aria-labelledby="home-title">
      <div className="hx-starfield home-stars" aria-hidden="true" />
      <div className="home-hero__grid">
        {/* grid frame: horizontals above/below the title, crossing dots, X marks */}
        <i className="home-hl home-hl--top" aria-hidden="true" />
        <i className="home-hl home-hl--bottom" aria-hidden="true" />
        <i className="home-x home-x--l" aria-hidden="true" />
        <i className="home-x home-x--r" aria-hidden="true" />
        <i className="home-dot home-dot--tl" aria-hidden="true" />
        <i className="home-dot home-dot--tr" aria-hidden="true" />
        <i className="home-dot home-dot--bl" aria-hidden="true" />
        <i className="home-dot home-dot--br" aria-hidden="true" />

        <div className="home-hero__top">
          <div className="hx-spotlight home-spot" aria-hidden="true" />
          <div className="home-logo home-rise" style={{ ["--step" as string]: 0 }}>
            <HelixLogo size={48} animated />
          </div>
          <p className="hx-eyebrow home-eyebrow home-eyebrow--lined home-rise" style={{ ["--step" as string]: 1 }}>
            Introducing
          </p>
        </div>

        <div className="home-hero__title">
          <h1 id="home-title" className="hx-wordmark home-wordmark home-rise" style={{ ["--step" as string]: 2 }}>
            HELIX
          </h1>
        </div>

        <div className="home-hero__bottom">
          <p className="hx-sub home-sub home-rise" style={{ ["--step" as string]: 3 }}>
            A living fraud immune system.
            <br />
            It senses, reasons and learns from every transaction.
          </p>
          <div className="home-rise" style={{ ["--step" as string]: 4 }}>
            <Cta />
          </div>
        </div>

        <div className="home-hero__cards home-rise" style={{ ["--step" as string]: 5 }}>
          <HeroCards hostRef={host} />
        </div>
      </div>
    </section>
  );
}

function Stages() {
  return (
    <section className="hx-section home-section" aria-labelledby="home-how">
      <Rule />
      <div className="home-container">
        <SectionHead
          id="home-how"
          eyebrow="The loop"
          title="How HELIX works"
          lede="Every transaction runs the same six stages, in order. Every investigator label feeds back into the next pass."
        />
        <div className="home-steps-wrap">
          <span className="home-steps__line" aria-hidden="true">
            <span className="home-steps__beam" />
          </span>
          <ol className="home-steps">
          {STAGES.map((s, i) => (
            <Reveal as="li" key={s.name} className="home-step" step={i}>
              <span className="home-step__node" aria-hidden="true">
                <Icon name={s.icon} size={18} />
              </span>
              <div className="hx-card home-step__card">
                <span className="home-step__num" aria-hidden="true">
                  {String(i + 1).padStart(2, "0")}
                </span>
                <h3 className="home-step__name">{s.name}</h3>
                <p className="home-step__text">{s.text}</p>
                <p className="home-step__ex">{s.example}</p>
              </div>
            </Reveal>
          ))}
          </ol>
        </div>
      </div>
    </section>
  );
}

function Features() {
  const { select } = useShellTab();
  return (
    <section className="hx-section home-section" aria-labelledby="home-features">
      <Rule />
      <div className="home-container">
        <SectionHead
          id="home-features"
          eyebrow="Inside"
          title="One system, every signal"
          lede="Specialist sensors, account memory, a transaction graph and a security layer feed one ranked, explained queue."
        />
        <ul className="home-bento">
          {FEATURES.map((f, i) => (
            <Reveal as="li" key={f.title} className={`hx-card home-feat home-feat--${f.size}`} step={i % 3}>
              <span className="home-feat__icon" aria-hidden="true">
                <Icon name={f.icon} />
              </span>
              <h3 className="home-feat__title">{f.title}</h3>
              <p className="home-feat__text">
                {f.lines[0]}
                <br />
                {f.lines[1]}
              </p>
              {f.visual}
              {f.tab ? (
                <button
                  type="button"
                  className="home-link"
                  aria-label={`View ${f.title} in the ${TAB_LABEL[f.tab]} tab`}
                  onClick={() => select(f.tab!)}
                >
                  View <span aria-hidden="true">→</span>
                </button>
              ) : null}
            </Reveal>
          ))}
        </ul>
      </div>
    </section>
  );
}

function Workflow() {
  const { select } = useShellTab();
  return (
    <section className="hx-section home-section" aria-labelledby="home-workflow">
      <Rule />
      <div className="home-container">
        <SectionHead id="home-workflow" eyebrow="Simulation" title="See the whole loop as a live 3D simulation" />
        <Reveal className="hx-card home-frame">
          <i className="home-frame__dot home-frame__dot--tl" aria-hidden="true" />
          <i className="home-frame__dot home-frame__dot--tr" aria-hidden="true" />
          <i className="home-frame__dot home-frame__dot--bl" aria-hidden="true" />
          <i className="home-frame__dot home-frame__dot--br" aria-hidden="true" />
          <WorkflowPreview />
          <div className="home-frame__bar">
            <p className="home-frame__caption">Customers, the HELIX core, receivers and investigators, animated from a scripted story or live events.</p>
            <button type="button" className="hx-pill home-pill" onClick={() => select("workflow")}>
              Open Workflow <span aria-hidden="true">→</span>
            </button>
          </div>
        </Reveal>
      </div>
    </section>
  );
}

function Adoption() {
  return (
    <section className="hx-section home-section" aria-labelledby="home-adopt">
      <Rule />
      <div className="home-container">
        <SectionHead id="home-adopt" eyebrow="Adoption" title="From pilot to production" />
        <ol className="home-phases">
          {PHASES.map((p, i) => (
            <Reveal as="li" key={p.name} className="hx-card home-phase" step={i}>
              <span className="home-phase__num" aria-hidden="true">
                {i + 1}
              </span>
              <h3 className="home-phase__name">{p.name}</h3>
              <p className="home-phase__text">{p.text}</p>
            </Reveal>
          ))}
        </ol>
        <Reveal className="home-note">
          <p>
            <strong>Honest scope.</strong> Today HELIX fits a small institution, fintech or pilot team: one product line, tens of thousands of
            accounts, one process. A large bank would also need horizontal scaling, a streaming bus, a replicated database, SSO with roles
            and model governance. None of that exists here yet.
          </p>
        </Reveal>
      </div>
    </section>
  );
}

function Built() {
  const items: [string, string][] = [
    [String(BUILT.stages), "pipeline stages"],
    [String(BUILT.sensorsWithSoc), "sensors with --soc"],
    [String(BUILT.properties), "correctness properties"],
    [BUILT.backendTests.toLocaleString("en-US"), "backend tests passed"],
    [BUILT.frontendTests.toLocaleString("en-US"), "frontend tests passed"],
  ];
  return (
    <section className="hx-section home-section home-section--tight" aria-labelledby="home-built">
      <Rule />
      <div className="home-container">
        <Reveal className="home-built">
          <h2 id="home-built" className="home-built__title">
            Built and tested
          </h2>
          <dl className="home-built__list">
            {items.map(([n, label]) => (
              <div key={label} className="home-built__item">
                <dt>{label}</dt>
                <dd>{n}</dd>
              </div>
            ))}
          </dl>
          <p className="home-built__note">Test counts at last full run, as recorded in the README.</p>
          <p className="home-built__note" data-testid="home-built-aml">
            IBM AMLworld HI-Small, 200,000 rows (one day of data): recall 0.045, precision 0.002, no laundering pattern detected yet. See
            the README Evaluation section.
          </p>
        </Reveal>
      </div>
    </section>
  );
}

function FinalCta() {
  return (
    <section className="home-final" aria-labelledby="home-final">
      <Rule />
      <div className="hx-spotlight home-spot home-spot--final" aria-hidden="true" />
      <Reveal className="home-final__inner">
        <h2 id="home-final" className="home-final__title">
          Ready to see it catch fraud?
        </h2>
        <p className="home-sub">Load the demo scenario on the Dashboard tab and watch T007 become Case C-1.</p>
        <Cta />
      </Reveal>
    </section>
  );
}

export function HomePage() {
  return (
    <div className="home" role="region" aria-label="Home" data-testid="home">
      <Hero />
      <LiveStrip />
      <Stages />
      <Features />
      <Workflow />
      <Adoption />
      <Built />
      <FinalCta />
      <div className="home-footer" data-testid="home-footer">
        <p>HELIX · hackathon prototype · synthetic and public benchmark data only · runs locally, no authentication.</p>
      </div>
    </div>
  );
}
