import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

/** `@/*` は apps/web 直下を指す（tsconfig.json の paths と揃える）。 */
const appRoot = fileURLToPath(new URL(".", import.meta.url));

export default defineConfig({
  resolve: {
    alias: [{ find: /^@\//, replacement: appRoot }],
  },
});
