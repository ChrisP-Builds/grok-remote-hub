/** @type {import('@playwright/test').PlaywrightTestConfig} */
const config = {
  testDir: "./tests/e2e",
  timeout: 60_000,
  retries: 0,
  use: {
    baseURL: process.env.HUB_URL || "http://127.0.0.1:8787",
    headless: true,
    screenshot: "only-on-failure",
    trace: "off",
  },
  reporter: [["list"]],
};

export default config;
