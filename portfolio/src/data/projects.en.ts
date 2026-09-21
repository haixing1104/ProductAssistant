import { demoPath } from "../lib/asset";
import type { Project } from "../types";

// =============================================================================
// 作品合集（**英文版**）：与 `data/projects.ts`（中文，事实源）**逐条对应**
//
// 为什么不把两种语言塞进同一个对象（`tagline: { zh, en }`）：
//   · 那样改一句话要在同一行里同时读两种语言，长文本（summary / highlights / clip.note）会很难校对；
//   · 本仓对平行内容已有先例且口径明确 —— `README.md` + `README_EN.md` 的「章节一一对应」，
//     这里照它办：两份文件各自可读，**结构对齐由门禁守**而不是靠人记。
//
// 三层门禁（漏一条都会被拦下，不会留到线上）：
//   1. **字段齐全**：`Project[]` 类型 + `__tests__/projectsEn.test.ts` 的非空断言；
//   2. **结构对齐**：数量 / slug / demos 顺序 / clip key 顺序 / 素材路径 / links / stack 必须与中文版一致
//      （素材路径一致 = 两种语言指向**同一批** `public/` 资产，不重复存图）；
//   3. **不得残留中文**：英文版任何一条文案里出现中日韩字符（含全角标点）即失败 —— 漏译的机械门禁。
//
// 术语口径与 `README_EN.md` 对齐（multi-tenant / human-in-the-loop / SSE typewriter /
// Reflection rewrite / compliance evaluation / platform port），避免同一件事在文档与页面上两种叫法。
// =============================================================================

export const projectsEn: Project[] = [
  {
    slug: "pa",
    name: "ProductAssistant — an AI copilot for product listings",
    tagline:
      "E-commerce content supply-chain workbench: pick a product → AI generates the listing copy and images → compliance evaluation → human-in-the-loop approval (with a third-party DingTalk notification) → published. One link, end to end.",
    summary:
      "A multi-tenant B2B console. The desktop console, the mobile H5 app and the React Native app share one contract core layer; " +
      "the backend is only a business gateway, while AI orchestration lives in a separate engine (LangGraph). " +
      "The two layers are kept apart by a Redis message stream and by their own PostgreSQL schemas — strongly decoupled, " +
      "so rewriting either layer in another language leaves the other one untouched.",
    period: "2025.09 – Present",
    status: "shipped",
    // 技术栈本就是英文（与中文版逐项相同，门禁会比对）：翻译它只会制造两套产品名
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
      "Multi-tenant isolation: 2 schemas + 4 database roles physically separate the AI layer from the business layer; every business query is funnelled through org_id (28 filtering points), and cross-tenant access is only possible for a platform super admin via a single override point",
      "Human-in-the-loop approval: approval CAS guards against concurrency, and the LangGraph checkpointer resumes across messages / processes / graph instances; a watchdog job covers failed redelivery",
      "Generation leaves a trail: SSE typewriter + stage and image events + the agent's reasoning trace + rejection comments fed into the next generation",
      "One contract layer for three clients: desktop / H5 / RN share api, services and store, with platform differences collapsed into 6 points on a platform port (API base URL / session redirect / expiry events / timers / JWT decoding / transport capabilities)",
      "AI imagery: image generation + OSS presigned direct upload + image normalisation and watermarking",
      "Engineering: idempotent migrations and seed scripts, a containerised database test suite, and type and test gates across the three clients (0 errors to pass)",
    ],
    // 入口地址与语言无关（同一套系统、两个入口）：必须与中文版逐字相同，门禁会比对
    links: {
      live: "https://seektruth.org.cn",
      liveH5: "https://seektruth.org.cn/m",
    },
    demos: [
      {
        platform: "web",
        // 端名两种语言一致（门禁会比对）：否则切语言时端 Tab 会改名，看起来像换了一个端
        label: "PC Web",
        aspect: "1882 / 912",
        clips: [
          {
            key: "01-list",
            title: "Product list and product creation",
            duration: "25s",
            note: "Workspace entry point: the multi-tenant product list, status filters and creating a product (field validation + draft state)",
            gif: demoPath("pa", "web", "01-list", "gif"),
          },
          {
            key: "02-generate",
            title: "AI generation and human rejection",
            duration: "153s",
            note: "The SSE typewriter generates copy and images section by section; submitting opens an approval, the reviewer rejects it with comments, and those comments feed the next generation",
            gif: demoPath("pa", "web", "02-generate", "gif"),
          },
          {
            key: "03-revise",
            title: "Rewrite from rejection comments",
            duration: "136s",
            note: "The AI reads the structured approval comments and rewrites the copy: the compliance score climbs from 62 to 95, and the resubmission passes automatically",
            gif: demoPath("pa", "web", "03-revise", "gif"),
          },
          {
            key: "04-blocked",
            title: "Restricted-word interception",
            duration: "39s",
            note: "What happens when a tenant's own restricted-word list is hit: it cannot be submitted until it is fixed, and the reason traces back to the exact word entry",
            gif: demoPath("pa", "web", "04-blocked", "gif"),
          },
        ],
      },
      {
        platform: "h5",
        label: "Mobile H5",
        aspect: "493 / 854",
        clips: [
          {
            key: "01-generate",
            title: "AI generation",
            duration: "87s",
            note: "The same contract layer on mobile: how the SSE typewriter, the stage events and the agent's reasoning trace lay out on a narrow screen",
            gif: demoPath("pa", "h5", "01-generate", "gif"),
          },
          {
            key: "02-approve",
            title: "Human approval",
            duration: "30s",
            note: "Approval list → detail → approve / reject (including the comment field), sharing the same approval CAS as the desktop console",
            gif: demoPath("pa", "h5", "02-approve", "gif"),
          },
        ],
      },
      {
        platform: "rn",
        label: "Mobile Native (Android & iOS)",
        aspect: "240 / 520",
        clips: [
          {
            key: "01-overview",
            title: "Native app, end to end",
            duration: "30s",
            note: "Android device (Expo dev build): create a product → AI streaming generation → compliance score 95 → submitted for approval automatically → approval queue",
            gif: demoPath("pa", "rn", "01-overview", "gif"),
          },
        ],
        note: "iOS needs macOS / Xcode or a physical iPhone to record; this machine runs Linux, so the clip is Android footage (both platforms share one codebase)",
      },
    ],
  },
];
