import { publicFile, resolveDemoSource } from "../lib/asset";
import type { DemoAsset } from "../types";

// 一个端的演示渲染。三种形态由 `resolveDemoSource()` 统一裁决（video > gif > 占位），
// 组件里不再各写一套 if —— 优先级只有一处定义，测试直接测那个函数。
export default function DemoPlayer({ asset }: { asset: DemoAsset }) {
  const source = resolveDemoSource(asset);

  if (source.kind === "video") {
    return (
      <div className="demo-frame mt-4">
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
      <div className="demo-frame mt-4">
        <img
          className="h-full w-full object-contain"
          src={publicFile(source.src)}
          alt={`${asset.label} 演示`}
          loading="lazy"
        />
      </div>
    );
  }

  // 占位态：刻意做成「一块设计好的空档」，而不是空白或坏图占位符
  return (
    <div className="demo-frame mt-4 flex flex-col items-center justify-center gap-2 bg-ink-50 px-6 text-center dark:bg-white/5">
      <span className="rounded-full bg-white px-3 py-1 text-xs font-medium text-brand-600 dark:bg-white/10 dark:text-brand-500">
        {asset.label}
      </span>
      <p className="max-w-md text-sm text-ink-700 dark:text-ink-100/80">{source.note}</p>
      <p className="text-xs text-ink-500 dark:text-ink-100/50">
        资产就位后此处自动替换为演示视频（见 portfolio/README.md）
      </p>
    </div>
  );
}
