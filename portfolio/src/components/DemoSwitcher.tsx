import { useState } from "react";
import type { KeyboardEvent } from "react";

import { resolveClip } from "../lib/asset";
import type { Platform, Project } from "../types";
import DemoPlayer from "./DemoPlayer";

// 三端演示切换（PC Web / Mobile H5 / Mobile Native）+ 端内**分段**。
//
// 关键取舍：**只挂载当前端、当前段的播放器**（靠 key 卸载上一个）。
//   · 好处一：多段动图同时播会掉帧发热，这里天然只有一个在动；
//   · 好处二：请求按需发出（未点开的端 / 段**不下载任何字节**）—— 动图动辄几 MB，这条最省流量；
//   · 代价：切回来会重新加载（可接受：每段只有 20–35s）。
//
// 分段卡片只在该端有 ≥2 段时出现：只有一段的端不该出现"只有一个选项的选择器"。
//
// **当前端是受控的**（`platform` + `onPlatformChange` 由 `ProjectCard` 传下来）：
// 卡片标题行右侧的「开始使用」入口要跟着当前端切目标，两处必须共用同一份状态。
type DemoSwitcherProps = {
  project: Project;
  /** 当前端（**受控**）：由 `ProjectCard` 持有 —— 卡片标题行的「开始使用」入口要按它切目标。 */
  platform?: Platform;
  onPlatformChange: (platform: Platform) => void;
};

export default function DemoSwitcher({ project, platform, onPlatformChange }: DemoSwitcherProps) {
  // 段用 key 记录而不是下标：将来插一段 / 调顺序，不会把访客停在"另一段"上。
  // 记录里带上"属于哪个端"，**换端即重置回第一段**由两条保障共同完成：
  //   · 点端 Tab 时顺手清空（见下面的 onClick）—— 覆盖"切走再切回"这种走回头路的情况
  //     （只靠比较的话，切回旧端会把上次那一段又恢复出来）；
  //   · 下面的派生比较 —— 覆盖"平台被外部改动"（父级将来给别的入口联动换端）：
  //     此时 clipKey 自动视为未选，不同端出现同名 key 也不会串台。
  // 两条都不需要 useEffect 里补一帧。
  const [selection, setSelection] = useState<{ platform?: Platform; key?: string }>({});
  const current = project.demos.find((demo) => demo.platform === platform) ?? project.demos[0];
  if (!current) return null;

  const clipKey = selection.platform === current.platform ? selection.key : undefined;
  const clip = resolveClip(current.clips, clipKey);
  const panelId = `${project.slug}-demo-panel`;
  const platformTabId = `${project.slug}-${current.platform}-tab`;
  const clipTabId = (key: string) => `${project.slug}-${current.platform}-${key}-tab`;
  const hasClips = current.clips.length > 1;
  // 分段卡片存在时，面板由"当前段"标记；只有一段（或没有素材）时退回端级 Tab
  const labelledBy = hasClips && clip ? clipTabId(clip.key) : platformTabId;

  /** ←/→ 切段 —— ARIA tab 的标准键盘行为。分段卡片就是 tab，所以焦点也一起移动。 */
  function handleClipKeys(event: KeyboardEvent<HTMLDivElement>) {
    const step = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
    if (step === 0 || !clip) return;

    event.preventDefault();
    const index = current.clips.findIndex((item) => item.key === clip.key);
    const next = current.clips[(index + step + current.clips.length) % current.clips.length];
    setSelection({ platform: current.platform, key: next.key });
    document.getElementById(clipTabId(next.key))?.focus();
  }

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
              onClick={() => {
                // 换端 = 忘掉上一端的段选择（"切走再切回"也回到第一段）。
                // 与 onPlatformChange 同批更新，不会多渲染一帧。
                setSelection({});
                onPlatformChange(demo.platform);
              }}
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

      {hasClips && (
        <div
          role="tablist"
          aria-label={`${current.label} 演示分段`}
          className="mt-4 flex flex-wrap gap-2"
          onKeyDown={handleClipKeys}
        >
          {current.clips.map((item) => {
            const selected = item.key === clip?.key;
            return (
              <button
                key={item.key}
                type="button"
                role="tab"
                id={clipTabId(item.key)}
                aria-selected={selected}
                aria-controls={panelId}
                onClick={() => setSelection({ platform: current.platform, key: item.key })}
                className={[
                  "inline-flex items-center gap-2 rounded-lg border px-3 py-1.5 text-xs font-medium transition outline-none",
                  "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-600",
                  selected
                    ? "border-brand-600 bg-brand-50 text-brand-700 dark:border-brand-500 dark:bg-brand-600/15 dark:text-brand-500"
                    : "border-ink-200 text-ink-700 hover:bg-ink-50 dark:border-white/15 dark:text-ink-50 dark:hover:bg-white/5",
                ].join(" ")}
              >
                {item.title}
                {/* 时长徽标：GIF 没有进度条也不能暂停，先让访客知道这段要看多久 */}
                <span className="rounded bg-white/70 px-1.5 py-0.5 text-[10px] tabular-nums text-ink-500 dark:bg-white/10 dark:text-ink-100/60">
                  {item.duration}
                </span>
              </button>
            );
          })}
        </div>
      )}

      <div role="tabpanel" id={panelId} aria-labelledby={labelledBy}>
        {/* key 里带上段：换段时卸载旧节点，动图/视频立即停止下载与播放 */}
        <DemoPlayer
          key={`${current.platform}/${clip?.key ?? "none"}`}
          clip={clip}
          label={current.label}
          aspect={current.aspect}
          note={current.note}
        />
      </div>

      {clip?.note && (
        <p className="mt-3 text-sm text-ink-700 dark:text-ink-100/80">
          <span className="font-medium text-ink-900 dark:text-ink-50">{clip.title}</span>：{clip.note}
        </p>
      )}
    </section>
  );
}

