/** True when the browser can create a WebGL context. The 3D network and the map need one;
 *  without it (old browsers, disabled GPU, jsdom) the panels show their text views only. */
export function hasWebGL(): boolean {
  if (typeof window === "undefined" || typeof (window as { WebGLRenderingContext?: unknown }).WebGLRenderingContext === "undefined") {
    return false;
  }
  try {
    const c = document.createElement("canvas");
    return !!(c.getContext("webgl2") ?? c.getContext("webgl"));
  } catch {
    return false;
  }
}
