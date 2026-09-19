import { useState } from "react";

import type { Platform, Project } from "../types";
import DemoPlayer from "./DemoPlayer";

// 四端演示切换。
//
// 关键取舍：**只挂载当前端的播放器**（靠 key 卸载上一个）。
//   · 好处一：4 个视频同时自动播在手机上会掉帧发热，这里天然只有一个在播；
//   · 好处二：视频请求按需发出（未点开的端不下载任何字节），首屏更快。
//   · 代价：切回来会重新加载（可接受：演示片段本来就短）。
export default function DemoSwitcher({ project }: { project: Project }) {
  const [active, setActive] = useState<Platform | undefined>(project.demos[0]?.platform);
  const current = project.demos.find((demo) => demo.platform === active) ?? project.demos[0];
  if (!current) return null;

  const panelId = `${project.slug}-demo-panel`;

  return (
    <section className="mt-8">
      <h4 className="text-xs font-medium tracking-widest text-ink-500 uppercase dark:text-ink-100/60">
        多端演示
      </h4>

      <div role="tablist" aria-label={`${project.name} 多端演示`} className="mt-3 flex flex-wrap gap-2">
        {project.demos.map((demo) => {
          const selected = demo.platform === current.platform;
          return (
            <button
              key={demo.platform}
              type="button"
              role="tab"
              id={`${project.slug}-${demo.platform}-tab`}
              aria-selected={selected}
              aria-controls={panelId}
              onClick={() => setActive(demo.platform)}
              className={[
                "inline-flex items-center justify-center rounded-full px-4 py-1.5 text-xs font-medium transition outline-none",
                "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-600",
                selected
                  ? "bg-brand-600 text-white"
                  : "border border-ink-200 text-ink-700 hover:bg-ink-50 dark:border-white/15 dark:text-ink-50 dark:hover:bg-white/5",
              ].join(" ")}
            >
              {demo.label}
            </button>
          );
        })}
      </div>

      <div role="tabpanel" id={panelId} aria-labelledby={`${project.slug}-${current.platform}-tab`}>
        <DemoPlayer key={current.platform} asset={current} />
      </div>
    </section>
  );
}
