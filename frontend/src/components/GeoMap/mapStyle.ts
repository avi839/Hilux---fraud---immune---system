// Offline base map. There is deliberately no tile server and no basemap library: the map is
// deck.gl alone over a plain background with a lat/lon graticule. Reasons:
// - Privacy: event coordinates never leave the browser. Tile requests reveal the viewport,
//   and so the area a Case's events are in, to a third party.
// - Offline: the demo runs with no internet access, so a remote style or tiles would fail.
// - Size: dropping MapLibre removes ~800 kB of JavaScript and ~65 kB of CSS from the map chunk.
// To add a real basemap later, put react-map-gl/maplibre under DeckGL with a self-hosted style.

/** Plain background behind the (transparent) deck.gl canvas. */
export const MAP_BACKGROUND = "#0a0a12";

/** One straight graticule segment, [lon, lat] to [lon, lat]. */
export interface GridLine {
  from: [number, number];
  to: [number, number];
}

/** Meridians and parallels every ``step`` degrees. In Web Mercator both are straight lines,
 *  so a single segment each is exact. Parallels stop at ±80° (Mercator diverges at the poles). */
export function graticule(step = 30): GridLine[] {
  const out: GridLine[] = [];
  for (let lon = -180; lon <= 180; lon += step) out.push({ from: [lon, -80], to: [lon, 80] });
  for (let lat = -60; lat <= 60; lat += step) out.push({ from: [-180, lat], to: [180, lat] });
  return out;
}
