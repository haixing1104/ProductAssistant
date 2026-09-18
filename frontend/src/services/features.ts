// 功能开关（共享层；桌面 / H5 / RN 三端都从这里取，避免"各写各的布尔量"漂移）。
//
// 为什么集中在这里而不是各页面内联常量:
//   「注册入口」出现在三个 App 的登录页里，若各写各的，就会出现"桌面开了、手机没开"
//   这类只在某端暴露的缺口。
//
// 当前策略（2026-09）: **不开放自助注册**。
//   注册的语义是「新开租户 + 该租户首个 admin」，属于把多租户数据面直接对外开放；
//   现阶段租户与账号由运维开通（见 backend-api `tools/seed_super_admin.py` 与
//   `database/sql/*.sql`）。恢复自助注册需要**同时**做两件事：
//     ① 这里改成 true（三端入口回来）；
//     ② 后端 `BACKEND_ALLOW_REGISTRATION=1`（否则接口仍是 403 —— 只开 UI 等于没开）。
export const SELF_REGISTRATION_ENABLED = false;
