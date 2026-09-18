/// <reference types="vitest/config" />
import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// 契约核心层（共享）与桌面端**同一份代码**：只有页面/组件是移动端自己的。
// 为什么这么共享（而不是复制）：http.ts（内存 access + 单飞续签 + 401 重放 + 流票据）
// 与 sse.ts（hitl.waiting 是终态 / ready 不重连 / 注释帧三态）是这个仓库历史上最容易流血的
// 两块，复制一份等于未来每次修复都要做两遍。共享层**零 antd 依赖**（实测），所以能直接跨目录
// 引用；页面壳（antd vs antd-mobile）本来就是两端各写各的。
const CORE_DIR = fileURLToPath(new URL("../frontend/src", import.meta.url));
/** 本项目自己的依赖目录（用于把共享层的依赖锁死到"同一份"，见下方 alias 注释）。 */
const selfDep = (name: string) => fileURLToPath(new URL(`./node_modules/${name}`, import.meta.url));

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      // 用法：@pa/core/api（→ frontend/src/api）、@pa/core/services/http、@pa/core/types/api…
      "@pa/core": CORE_DIR,
      // **必须把这些依赖锁到本项目副本**：共享层（frontend/src/**）内部 `import react/zustand/axios`
      // 会按**它自己的目录**解析 → 解析到 `frontend/node_modules`，于是同一个页面加载**两份 React**，
      // 症状是 hooks 直接崩：`Cannot read properties of null (reading 'useCallback')`
      //（单测与浏览器同错，实测）。绝对路径别名在 Vite 与 Vitest 两条解析路径上都生效。
      react: selfDep("react"),
      "react-dom": selfDep("react-dom"),
      zustand: selfDep("zustand"),
      axios: selfDep("axios"),
      "@tanstack/react-query": selfDep("@tanstack/react-query"),
    },
    // 双保险：即便有依赖绕过上面的别名，也强制 react/react-dom 从本项目解析
    dedupe: ["react", "react-dom"],
  },
  server: {
    port: 5174,
    // 端口被占时立即报错（默认会静默改用 5175，导致「起了但访问 5174 超时」的误导）
    strictPort: true,
    // 真机联调：手机访问 http://<本机局域网IP>:5174（走下面的 /api 代理，因此**不需要 CORS**）
    host: true,
    // WSL / 网络盘（/mnt/c）inotify 不可靠：轮询监听
    watch: { usePolling: true },
    // 关键：核心层在 mobile-h5 根目录之外 —— 不放行会 403（dev server 的 fs 白名单）
    fs: { allow: [".", "../frontend"] },
    // 前端只访问 backend-api（模块红线）；生产由边缘 nginx 同源反代
    proxy: {
      // SSE 长连接：被代理攒数据会毁掉打字机（同桌面端 vite.config.ts）
      "/api": { target: "http://localhost:8000", changeOrigin: true },
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    globals: true,
    // 门禁范围 = 移动端自己的薄逻辑层（纯函数/视图模型）。
    // 两个「移动端共用文件」（`../frontend/src/services/{mobileFormat,streamLabels}.ts`）**不在这里**：
    // v8 覆盖默认不统计 root 之外的文件（`allowExternal` 实测也不生效），
    // 它们的用例因此放在共享层 `frontend/src/__tests__/`，门禁由桌面端套件承担。
    coverage: {
      provider: "v8",
      reporter: ["text", "html"],
      include: ["src/services/**", "src/api/**"],
      exclude: ["src/**/*.test.{ts,tsx}"],
      thresholds: { statements: 80, branches: 75, functions: 75, lines: 80 },
    },
  },
});
