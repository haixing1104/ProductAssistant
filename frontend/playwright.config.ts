// E2E（默认全量 skip）：需完整 dev 栈（backend-api + 前端 + 真实 PG/Redis）。
// 启用方式：PA_E2E=1 npx playwright test（前置：backend 跑在 8000、vite 跑在 5173）
import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  use: { baseURL: "http://localhost:5173" },
  webServer: {
    command: "npm run dev",
    url: "http://localhost:5173",
    reuseExistingServer: true,
  },
});
