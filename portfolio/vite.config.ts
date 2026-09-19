/// <reference types="vitest/config" />
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// 作品集宣传页（静态作品合集，P11）。
//
// 与 frontend/ mobile-h5/ 的三处刻意差异（都有理由，别照抄）:
//   1. **没有 /api 代理** —— 本模块纯静态，零后端依赖（不调 backend-api 的任何端点），
//      因此它可以被扔到任意静态托管（GitHub Pages / CF Pages / OSS / nginx）而不用配反代；
//   2. **Tailwind v4 插件只在这里注册** —— 原子化 CSS 是本模块的局部选择，
//      frontend/（antd）、mobile-h5/（antd-mobile）、mobile-rn/（自研薄 UI）的构建链路零改动；
//   3. **深色模式跟系统**（v4 默认 prefers-color-scheme）—— 不做切换按钮，少一个状态与 FOUC 风险。
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    // 端口占用即报错（不许静默换端口）：5173 = frontend、5174 = mobile-h5、5175 = 本模块
    port: 5175,
    strictPort: true,
    // WSL / 网络盘 inotify 不可靠：轮询监听（与 frontend/ mobile-h5/ 同口径）
    watch: { usePolling: true },
  },
  build: {
    // 单页静态站，产物越小越好：指定目录便于验收时直接看体积
    outDir: "dist",
    sourcemap: false,
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    globals: true,
    // 覆盖率门禁（口径与 frontend/ 一致）：只覆盖「薄逻辑层」——
    // 数据（内容事实源）与 lib（纯函数）。UI 组件受 jsdom + 原子类约束不进统计，
    // 由 homePage.test.tsx 渲染冒烟守护。
    coverage: {
      provider: "v8",
      reporter: ["text", "html"],
      include: ["src/data/**", "src/lib/**"],
      exclude: ["src/**/*.test.{ts,tsx}", "src/test/**"],
      // 与 frontend/ 相同的门槛值：挡住「大面积无覆盖」，又不会因为多写一行就卡住
      thresholds: { statements: 80, branches: 75, functions: 75, lines: 80 },
    },
  },
});
