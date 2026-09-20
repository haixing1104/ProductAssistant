/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // 端口被占时立即报错（默认会静默改用 5174，导致「dev 起了但访问 5173 超时」的误导）
    strictPort: true,
    // WSL / 网络盘（/mnt/c）inotify 不可靠：轮询监听，避免改文件后 dev server 不生效
    watch: { usePolling: true },
    // 前端只访问 backend-api（模块红线）；本地代理避免 CORS（生产由 nginx 同源反代，见 P8）
    proxy: {
      // 必须 ws:false + 关缓冲：SSE 长连接被代理攒数据会毁掉打字机（见 services/sse.ts）
      "/api": { target: "http://localhost:8000", changeOrigin: true },
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    globals: true,
    // CI runner 比本地慢 2~3 倍：默认 5s 对「antd 渲染 + userEvent 交互 + findBy* 轮询」这类用例
    // 太紧 —— 2026-09-20 实测 approvalsPage.test.tsx 的「带评估命中点的单：放行必须写理由（未填时
    // 确认按钮禁用）」本地 2204ms，CI 上直接 Test timed out in 5000ms（该用例内部本来就有多个
    // findBy*(timeout: 5000)，单用例总预算必须大于这些小超时之和）。20s 仍能挡住"真卡死"。
    testTimeout: 20000,
    // 只收集 src 下的用例：e2e/ 交给 playwright（frontend/playwright.config.ts）。
    // 不加这行时 vitest 的默认 include 会把 e2e/login-flow.spec.ts 也当成单测收进来，
    // 然后在 worker 里于**模块顶层**调用 Playwright 的 test.skip(...) → 直接抛
    // "test.skip() can only be called inside test, describe block or fixture"，
    // 于是 `npm test` 必红、`frontend 测试与构建` 这个 job 一直过不去（2026-09-20 实测）。
    include: ["src/**/*.{test,spec}.{ts,tsx}"],
    // 覆盖率门禁（与 backend/ai-engine 对等）：只覆盖「薄逻辑层」（store / 服务 / 纯函数）。
    // UI 页面受 jsdom + antd 约束**不进覆盖率统计**，由真实全栈冒烟 + E2E 守护；
    // 例外：ProductDetailPage / OpsPage 各有一份**页面级回归用例**（生成卡住事故 + 终止卡住任务），
    // 只在必要时加、只断言交互语义（连流次数 / 按钮态 / 请求体），不追求渲染细节覆盖。
    coverage: {
      provider: "v8",
      reporter: ["text", "html"],
      include: ["src/api/**", "src/services/**", "src/store/**"],
      // 两个「移动端共用」文件（`src/services/mobileFormat.ts` / `streamLabels.ts`）**不做 exclude**：
      // 它们的用例已经在共享层（`src/__tests__/mobileFormat.test.ts` / `streamLabels.test.ts`），
      // 由本套件统一守护 —— 这是"测试在哪、门禁在哪"的落点（H5 侧 v8 覆盖统计不到 root 外文件，实测）。
      exclude: ["src/**/*.test.{ts,tsx}"],
      // 基线（2026-09 首版）：stmts 88 / branch 82 / func 87 / lines 89 —— 门槛取略低于基线的值，
      // 避免「多写一行 UI 逻辑就卡 CI」，同时挡住「大面积无覆盖」的回归。
      thresholds: { statements: 80, branches: 75, functions: 75, lines: 80 },
    },
  },
});
