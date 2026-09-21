import { isPortrait, publicFile, resolveDemoSource } from "../lib/asset";
import type { DemoClip } from "../types";

type DemoPlayerProps = {
  /** 当前生效的这一段；`undefined` = 这一端还没录（渲染占位卡）。 */
  clip?: DemoClip;
  /** 端名，用于 `alt` 与占位卡上的标签。 */
  label: string;
  /** 舞台外框比例（CSS 值），来自数据里素材的真实尺寸。 */
  aspect: string;
  /** 整端还没录时的说明。 */
  note?: string;
};

// 一段演示的渲染。三种形态由 `resolveDemoSource()` 统一裁决（video > gif > 占位），
// 组件里不再各写一套 if —— 优先级只有一处定义，测试直接测那个函数。
//
// 外框比例走数据里的 `aspect`（素材真实尺寸）而不是写死 16:9：
//   · 桌面录屏 1882×912 是 2.06:1，套 16:9 会留黑边；
//   · 手机截图 493×854 是竖屏，套 16:9 会被压成"矮胖"，文字糊成一团。
// 竖屏端再加 `demo-frame--phone` 收窄居中（见 styles/app.css）。
export default function DemoPlayer({ clip, label, aspect, note }: DemoPlayerProps) {
  const source = resolveDemoSource(clip ?? { note });
  const frameClass = `demo-frame mt-4${isPortrait(aspect) ? " demo-frame--phone" : ""}`;

  if (source.kind === "video") {
    return (
      <div className={frameClass} style={{ aspectRatio: aspect }}>
        {/*
          muted + playsInline + loop 是**自动播放的前提**：少任何一个，浏览器会静默拒绝自动播，
          页面表现成「视频停在第一帧、点了才有反应」——这个坑不报错，只表现为"看起来没录好"。
          controls 让访客能暂停/回看；poster 是首帧图，加载期间不留白块。
          object-contain 而不是 cover：演示里全是界面文字，裁掉一部分比留点黑边糟糕得多。
        */}
        <video
          className="h-full w-full object-contain"
          src={publicFile(source.src)}
          poster={source.poster ? publicFile(source.poster) : undefined}
          muted
          loop
          playsInline
          autoPlay
          controls
          preload="metadata"
        />
      </div>
    );
  }

  if (source.kind === "gif") {
    return (
      <div className={frameClass} style={{ aspectRatio: aspect }}>
        <img
          className="h-full w-full object-contain"
          src={publicFile(source.src)}
          alt={`${clip?.title ?? label} 演示`}
          loading="lazy"
        />
      </div>
    );
  }

  // 占位态：刻意做成「一块设计好的空档」，而不是空白或坏图占位符
  return (
    <div
      className={`${frameClass} flex flex-col items-center justify-center gap-2 bg-ink-50 px-6 text-center dark:bg-white/5`}
      style={{ aspectRatio: aspect }}
    >
      <span className="rounded-full bg-white px-3 py-1 text-xs font-medium text-brand-600 dark:bg-white/10 dark:text-brand-500">
        {label}
      </span>
      <p className="max-w-md text-sm text-ink-700 dark:text-ink-100/80">{source.note}</p>
      <p className="text-xs text-ink-500 dark:text-ink-100/50">
        资产就位后此处自动替换为演示动图 / 视频
      </p>
    </div>
  );
}

