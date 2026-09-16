import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  timeout: 30_000,
  use: {
    baseURL: "http://127.0.0.1:8765",
    screenshot: "only-on-failure",
  },
  webServer: {
    command:
      "CODING_AGENT_API_KEY=playwright-only CODING_AGENT_MODEL=test-model " +
      "uv run --project .. coding-agent web " +
      "--config /tmp/coding-agent-playwright-config.json --no-langsmith --port 8765",
    cwd: ".",
    url: "http://127.0.0.1:8765/api/v1/status",
    reuseExistingServer: true,
    timeout: 30_000,
  },
});
