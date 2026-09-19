// 极简 Toast：本模块**没有**通知系统，也不为此引一个库 —— 目前唯一的用途是
// 「原生端暂不支持下载」这类"点了要解释一句"的提示。
//
// `role="status"` 自带 `aria-live="polite"`：屏幕阅读器会播报，不需要再补 aria-* 属性。
// 出现与消失由调用方的定时器控制（见 `ProjectCard`），本组件自身无状态、无副作用。
export default function Toast({ message }: { message: string }) {
  return (
    <div
      role="status"
      className="fixed bottom-6 left-1/2 z-50 max-w-[calc(100vw-2rem)] -translate-x-1/2 rounded-full bg-ink-900 px-4 py-2 text-center text-sm text-white shadow-lg dark:bg-ink-50 dark:text-ink-900"
    >
      {message}
    </div>
  );
}
