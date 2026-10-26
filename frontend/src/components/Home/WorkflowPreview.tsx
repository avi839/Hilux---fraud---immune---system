// Lightweight loop diagram for the Home page: payments flow from customers through the HELIX
// ring to receivers (cleared) or to an investigator (flagged), whose labels flow back. Pure
// SVG + CSS (stroke-dash animation), no three.js. Every path is named in text, so nothing
// depends on colour. Animation is CSS-only and off under prefers-reduced-motion.

const CUSTOMERS = [96, 148, 200, 252, 304];
const RING = { x: 500, y: 200 };

const inPath = (y: number) => `M118 ${y} C 290 ${y}, 330 ${RING.y}, 432 ${RING.y}`;
const OUT_CLEAR = `M568 ${RING.y} C 690 ${RING.y}, 720 112, 846 112`;
const OUT_FLAG = `M568 ${RING.y} C 690 ${RING.y}, 720 288, 846 288`;
const BACK = `M846 304 C 720 360, 590 352, 528 262`;

export function WorkflowPreview() {
  return (
    <svg className="home-flow" viewBox="0 70 1000 320" role="img" aria-labelledby="home-flow-desc" focusable="false">
      <desc id="home-flow-desc">
        Diagram: payments from customers flow into the HELIX loop. Normal payments continue to receivers; suspicious ones become Cases for
        an investigator, whose labels flow back into HELIX.
      </desc>
      <defs>
        <radialGradient id="home-flow-core" cx="50%" cy="50%" r="50%">
          <stop offset="0" stopColor="#c7d2fe" stopOpacity="0.35" />
          <stop offset="1" stopColor="#c7d2fe" stopOpacity="0" />
        </radialGradient>
      </defs>

      {CUSTOMERS.map((y, i) => (
        <g key={y}>
          <path className="home-flow__rail" d={inPath(y)} />
          <path className="home-flow__dots" d={inPath(y)} style={{ animationDelay: `${-i * 0.7}s` }} />
          <circle className="home-flow__cust" cx={100} cy={y} r={7} />
        </g>
      ))}
      <text className="home-flow__label" x={100} y={350} textAnchor="middle">
        Customers
      </text>

      <path className="home-flow__rail" d={OUT_CLEAR} />
      <path className="home-flow__dots" d={OUT_CLEAR} />
      <path className="home-flow__rail" d={OUT_FLAG} />
      <path className="home-flow__dots home-flow__dots--flag" d={OUT_FLAG} />
      <path className="home-flow__rail home-flow__rail--back" d={BACK} />
      <path className="home-flow__dots home-flow__dots--back" d={BACK} />

      <circle cx={RING.x} cy={RING.y} r={120} fill="url(#home-flow-core)" />
      <g className="home-flow__spin">
        <circle className="home-flow__orbit" cx={RING.x} cy={RING.y} r={84} />
      </g>
      <circle className="home-flow__ring" cx={RING.x} cy={RING.y} r={64} />
      <text className="home-flow__core" x={RING.x} y={RING.y - 2} textAnchor="middle">
        HELIX
      </text>
      <text className="home-flow__sub" x={RING.x} y={RING.y + 18} textAnchor="middle">
        sense · reason · act
      </text>

      <g>
        <rect className="home-flow__end" x={846} y={92} width={124} height={40} rx={20} />
        <text className="home-flow__label" x={908} y={117} textAnchor="middle">
          Receivers
        </text>
        <text className="home-flow__sub" x={908} y={152} textAnchor="middle">
          cleared payments
        </text>
      </g>
      <g>
        <rect className="home-flow__end home-flow__end--flag" x={846} y={268} width={124} height={40} rx={20} />
        <text className="home-flow__label" x={908} y={293} textAnchor="middle">
          Investigator
        </text>
        <text className="home-flow__sub" x={908} y={328} textAnchor="middle">
          flagged Cases
        </text>
      </g>
      <text className="home-flow__sub" x={690} y={372} textAnchor="middle">
        labels flow back · LEARN
      </text>
    </svg>
  );
}
