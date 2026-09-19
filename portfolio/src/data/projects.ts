import { demoPath } from "../lib/asset";
import type { Project } from "../types";

// =============================================================================
// 作品合集（**唯一内容事实源**）
//
// 加一个新作品：复制下面一个对象 → 改 slug/name/tagline/… → 建 `public/demos/<slug>/` 目录。
// 页面（卡片、三端演示切换、进入系统按钮）全部由这份数据驱动，不需要改任何组件。
//
// 每一条都会被 `__tests__/projects.test.ts` 校验，其中三条最值得留意：
//   1. **声明了 gif/video/poster 就必须在 public/ 下真实存在**（文件名写错 = 测试红）；
//   2. 每个端都要有 `clips`（段）与 `aspect`（= 素材真实尺寸，决定舞台外框比例）；
//   3. 某个端没有素材时，`clips` 留空数组并给 `note`（否则页面上是一块空白，而不是设计好的占位）。
//
// 段的粒度口径：**一段 = 一个动作**（建品 / 生成 / 审批 / 拦截），20–35s。
// 动图不能暂停也不能拖进度，把 2 分钟的完整流程塞成一段，访客只会看到开头几秒。
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
        label: "PC Web",
        // 素材真实尺寸 1882×912（录制窗口），外框按这个比例走，不裁不留黑边
        aspect: "1882 / 912",
        clips: [
          {
            key: "01-list",
            title: "商品列表与建品",
            duration: "25s",
            note: "工作台入口：多租户商品列表、状态筛选与新建商品（字段校验 + 草稿态）",
            gif: demoPath("pa", "web", "01-list", "gif"),
          },
          {
            key: "02-generate",
            title: "AI 生成与人工审批驳回",
            duration: "153s",
            note: "SSE 打字机逐段生成文案与配图，提交后进入审批中心；审批人带意见驳回，驳回意见回灌下一次生成",
            gif: demoPath("pa", "web", "02-generate", "gif"),
          },
          {
            key: "03-revise",
            title: "按驳回意见改写",
            duration: "136s",
            note: "AI 读取结构化审批意见重写文案，合规分从 62 提到 95，再次提交后自动通过",
            gif: demoPath("pa", "web", "03-revise", "gif"),
          },
          {
            key: "04-blocked",
            title: "违规词拦截",
            duration: "39s",
            note: "命中租户自定义违规词库时的拦截与提示：不改不能提交，拦截原因可追溯到词条",
            gif: demoPath("pa", "web", "04-blocked", "gif"),
          },
        ],
      },
      {
        platform: "h5",
        label: "Mobile H5",
        // 素材真实尺寸 493×854（iPhone 视口）→ 竖屏，舞台收窄居中
        aspect: "493 / 854",
        clips: [
          {
            key: "01-generate",
            title: "AI 生成",
            duration: "87s",
            note: "移动端同一份契约层：SSE 打字机、阶段事件与 AI 思考轨迹在窄屏下的排版",
            gif: demoPath("pa", "h5", "01-generate", "gif"),
          },
          {
            key: "02-approve",
            title: "人工审批",
            duration: "30s",
            note: "审批列表 → 详情 → 批准 / 驳回（含审批意见输入），与桌面端共用同一套审批 CAS",
            gif: demoPath("pa", "h5", "02-approve", "gif"),
          },
        ],
      },
      {
        // Android 与 iOS 合成一个 Tab：原生端一套代码两端，素材覆盖两者。
        // iOS 的录制条件（macOS / Xcode 或 iPhone 真机）写在 note 里，不单独占一个永远空的 Tab。
        platform: "rn",
        label: "Mobile Native (Android & iOS)",
        // 素材真实尺寸 240×520（Android 真机录屏重编码后）→ 竖屏
        aspect: "240 / 520",
        clips: [
          {
            key: "01-overview",
            title: "原生 App 全链路",
            duration: "30s",
            note: "Android 真机（Expo dev build）：建品 → AI 流式生成 → 合规评分 95 → 自动提审 → 审批中心待办",
            gif: demoPath("pa", "rn", "01-overview", "gif"),
          },
        ],
        note: "iOS 端需 macOS / Xcode 或 iPhone 真机录制；本机为 Linux，故这一段为 Android 真机素材（两端共用一套代码）",
      },
    ],
  },
];

// ── 录好一段演示后的写法 ────────────────────────────────────────────────────
//
// 1) 素材按 `demos/<slug>/<platform>/<key>.<ext>` 命名（`demoPath()` 生成，别手写）：
//      public/demos/pa/web/01-list.gif
// 2) 在对应端的 `clips` 里加一段：key / title / duration / note / gif(或 video + poster)。
// 3) 段的 key 用 `01-`、`02-` 前缀：分段卡片按数组顺序渲染，文件名也就能一眼看出先后。
//
// 换成 mp4（体积只有 GIF 的 1/5–1/10，还能暂停）时只改这两行，组件不用动：
//
//   video: demoPath("pa", "web", "01-list", "mp4"),
//   poster: demoPath("pa", "web", "01-list", "webp"),

