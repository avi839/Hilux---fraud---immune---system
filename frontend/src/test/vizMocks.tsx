// Stand-ins for the WebGL libraries, installed for every test by setup.ts: jsdom has no
// WebGL, so react-force-graph-3d and deck.gl cannot run here. The mocks record the props
// they receive so tests can check what the panels pass and invoke callbacks (e.g. a node
// click) the way the real library would.

import type { ReactNode } from "react";

export const vizCalls = {
  forceGraph: [] as Array<Record<string, unknown>>,
  deck: [] as Array<Record<string, unknown>>,
  reset() {
    this.forceGraph.length = 0;
    this.deck.length = 0;
  },
  lastForceGraph() {
    return this.forceGraph[this.forceGraph.length - 1];
  },
  lastDeck() {
    return this.deck[this.deck.length - 1];
  },
};

export function MockForceGraph(props: Record<string, unknown>) {
  vizCalls.forceGraph.push(props);
  const data = props.graphData as { nodes: unknown[]; links: unknown[] } | undefined;
  return <div data-testid="mock-force-graph" data-nodes={data?.nodes.length ?? 0} data-links={data?.links.length ?? 0} />;
}

class MockLayer {
  readonly id: string;
  readonly props: Record<string, unknown>;
  constructor(props: Record<string, unknown>) {
    this.props = props;
    this.id = String(props.id);
  }
}
export class MockScatterplotLayer extends MockLayer {
  readonly kind = "ScatterplotLayer";
}
export class MockLineLayer extends MockLayer {
  readonly kind = "LineLayer";
}

export class MockWebMercatorViewport {
  longitude = 0;
  latitude = 0;
  zoom = 1;
  constructor(_opts: { width: number; height: number }) {}
  fitBounds(b: [[number, number], [number, number]]) {
    const v = new MockWebMercatorViewport({ width: 0, height: 0 });
    v.longitude = (b[0][0] + b[1][0]) / 2;
    v.latitude = (b[0][1] + b[1][1]) / 2;
    v.zoom = 3;
    return v;
  }
}

export function MockDeckGL(props: Record<string, unknown> & { children?: ReactNode }) {
  vizCalls.deck.push(props);
  return <div data-testid="mock-deck">{props.children}</div>;
}
