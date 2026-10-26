// Minimal typing for the one Node API the tests use (the project does not depend on
// @types/node). Vitest runs tests in Node, so node:fs is available there; app code must
// not import it.
declare module "node:fs" {
  export function readFileSync(path: string, encoding: "utf8"): string;
}
