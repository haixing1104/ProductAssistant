import type { Project } from "../types";

// =============================================================================
// 作品合集（**唯一内容事实源**）
//
// 加一个新作品：复制下面一个对象 → 改 slug/name/tagline/… → 建 `public/demos/<slug>/` 目录。
// 页面（卡片、四端演示切换、进入系统按钮）全部由这份数据驱动，不需要改任何组件。
//
// 每一条都会被 `__tests__/projects.test.ts` 校验，其中两条最值得留意：
//   1. **声明了 video/gif/poster 就必须在 public/ 下真实存在**（文件名写错 = 测试红）；
//   2. 某个端没有 video/gif 时必须给 `note`（否则页面上是一块空白，而不是设计好的占位）。
// =============================================================================

export const projects: Project[] = [
  {
    slug: "pa",
    name: "ProductAssistant 产品上线助手",
    tagline: "电商内容供应链工作台：选品 → AI 生成 → 合规校验 → 人工审批 → 上线，一条链路跑完",
    summary:
      "面向多租户的 B 端工作台。桌面工作台、移动端 H5 与 React Native 原生 App 三端共用一份契约核心层；" +
      "后端只做业务网关，AI 编排独立成一个引擎（LangGraph），两层用 Redis 消息流 + PostgreSQL 各自 schema 隔开，" +
      "任意一层换语言重写都不影响另一层。",
    period: "2025.09 – 至今",
    status: "shipped",
    stack: [
      "React 19",
      "TypeScript",
      "Vite 8",
      "Ant Design 6",
      "React Native (Expo 57)",
      "FastAPI",
      "LangGraph",
      "PostgreSQL",
      "Redis",
      "Milvus",
      "Docker",
    ],
    highlights: [
      "多租户隔离：2 个 schema + 4 个数据库角色把 AI 层与业务层物理隔开；业务查询全部按 org_id 收口（28 处过滤点），跨租户只允许平台超管经单一覆盖点进入",
      "人在环审批（HITL）：审批 CAS 防并发 + LangGraph checkpointer 跨消息 / 跨进程 / 跨图实例恢复，补投失败另有守护任务兜底",
      "生成即留痕：SSE 打字机 + 阶段与配图事件 + AI 思考轨迹 + 驳回意见回灌下一次生成",
      "三端一套契约层：桌面 / H5 / RN 共用 api、services、store，平台差异收敛到「平台端口」的 6 个点（接口基址 / 会话回跳 / 过期事件 / 定时器 / JWT 解码 / 传输能力）",
      "AI 配图：生图 + OSS 预签名直传 + 图片规格化与水印",
      "工程化：幂等迁移与种子脚本、容器化数据库测试套件、三端类型与用例门禁（0 错误才算过）",
    ],
    links: {
      // ── 演示环境落地后取消注释即可（组件无需改动，「进入系统」立刻变为真链接）──
      // live: "https://demo.example.com",
      // repo: "https://github.com/<your-account>/ProductAssistant",
    },
    demos: [
      {
        platform: "web",
        label: "Web 工作台",
        note: "录制中：商品列表 → 详情页 SSE 打字机 → 审批中心",
      },
      {
        platform: "h5",
        label: "移动端 H5",
        note: "录制中：审批列表 → 通知深链 → 批准 / 驳回",
      },
      {
        platform: "rn-android",
        label: "RN · Android",
        note: "录制中：原生 App（Expo dev build / 真机）",
      },
      {
        platform: "rn-ios",
        label: "RN · iOS",
        note: "iOS 端需 macOS / Xcode 或真机录制，暂以 Android 版演示",
      },
    ],
  },
];

// 录好一段演示后的写法（资产命名与压缩命令见 `public/demos/pa/README.md`）：
//
//   import { demoPath } from "../lib/asset";   // ← 记得在文件顶部补这行
//   …
//   {
//     platform: "web",
//     label: "Web 工作台",
//     video: demoPath("pa", "web", "mp4"),
//     poster: demoPath("pa", "web", "webp"),
//     note: "商品列表 → 详情页 SSE 打字机 → 审批中心",
//   }
//
// `demoPath` 保证命名统一；写错名字（或忘了放文件）会在 `npm test` 里被当场指出 ——
// 这条「声明即校验」的规则就是为了挡住「上线后才发现是个白块」。
