import { existsSync } from "node:fs";
import { join, resolve } from "node:path";

import { projects } from "../data/projects";
import { aspectRatio, PLATFORMS } from "../lib/asset";
import type { Platform } from "../types";

// public/ 的绝对路径。
// 为什么不用 `import.meta.url`：本仓的 vitest 在 jsdom 环境下模块 URL **不是** file: 协议
// （实测抛 `TypeError: The URL must be of scheme file`），因此用 cwd —— `npm test` 与
// `../scripts/test-landing.sh` 都在 portfolio/ 下执行。断言里会把解析出的路径打出来，
// 万一在别的目录下跑，报错信息会直接告诉你它找了哪里。
const PUBLIC_DIR = resolve(process.cwd(), "public");

const ASSET_FIELDS = ["video", "gif", "poster"] as const;

/**
 * 需求钉死：`pa` 三端各有几段演示（PC Web 4 / Mobile H5 2 / Mobile Native 1）。
 *
 * 故意**不复用数据推出**的数量：谁把段删了、或加了段忘了配素材，都该在这里被发现，
 * 而不是等访客看到"少了一段"才发现。加段属于需求变更 —— 先改这里，再改数据。
 */
const EXPECTED_CLIP_COUNTS: Record<string, Partial<Record<Platform, number>>> = {
  pa: { web: 4, h5: 2, rn: 1 },
};

describe("作品数据（src/data/projects.ts）", () => {
  it("至少有一个作品，且 slug 唯一（slug 同时是资产目录名）", () => {
    expect(projects.length).toBeGreaterThan(0);

    const slugs = projects.map((project) => project.slug);
    expect(new Set(slugs).size).toBe(slugs.length);
    for (const slug of slugs) {
      expect(slug).toMatch(/^[a-z0-9][a-z0-9-]*$/);
    }
  });

  it("每个作品的展示字段都填齐了（空白页比报错更难发现）", () => {
    for (const project of projects) {
      expect(project.name.trim(), `${project.slug}: name 为空`).not.toBe("");
      expect(project.tagline.trim(), `${project.slug}: tagline 为空`).not.toBe("");
      expect(project.summary.trim(), `${project.slug}: summary 为空`).not.toBe("");
      expect(project.period.trim(), `${project.slug}: period 为空`).not.toBe("");
      expect(project.stack.length, `${project.slug}: stack 为空`).toBeGreaterThan(0);
      expect(project.highlights.length, `${project.slug}: highlights 为空`).toBeGreaterThan(0);
      expect(["shipped", "in-progress"]).toContain(project.status);
    }
  });

  it("每个作品的端合法、不重复、都有 Tab 名字", () => {
    for (const project of projects) {
      const platforms = project.demos.map((demo) => demo.platform);
      expect(new Set(platforms).size, `${project.slug}: 同一个端配了多条演示`).toBe(platforms.length);

      for (const demo of project.demos) {
        expect(PLATFORMS, `${project.slug}: 端名拼错 → ${demo.platform}`).toContain(demo.platform);
        expect(demo.label.trim(), `${project.slug}/${demo.platform}: label 为空`).not.toBe("");
      }
    }
  });

  it("每个端都有 clips 数组与 aspect（舞台比例，来自素材真实尺寸）", () => {
    for (const project of projects) {
      for (const demo of project.demos) {
        const where = `${project.slug}/${demo.platform}`;
        expect(Array.isArray(demo.clips), `${where}: clips 必须是数组`).toBe(true);
        expect(demo.aspect, `${where}: aspect 必须写成 "宽 / 高"（如 "1882 / 912"）`).toMatch(/^\d+\s*\/\s*\d+$/);
        expect(aspectRatio(demo.aspect), `${where}: aspect 解析不出正的比例 → ${demo.aspect}`).toBeGreaterThan(0);
      }
    }
  });

  it("段内字段填齐：key 唯一且形如 01-list、title / duration 非空、至少有 gif 或 video", () => {
    for (const project of projects) {
      for (const demo of project.demos) {
        const keys = demo.clips.map((clip) => clip.key);
        expect(new Set(keys).size, `${project.slug}/${demo.platform}: 有重复的段 key`).toBe(keys.length);

        for (const clip of demo.clips) {
          const where = `${project.slug}/${demo.platform}/${clip.key}`;
          // 前缀就是段序（分段卡片按数组顺序渲染，文件名也就能一眼看出先后）
          expect(clip.key, `${where}: 段 key 应该形如 01-list`).toMatch(/^0[1-9]-[a-z0-9]+(-[a-z0-9]+)*$/);
          expect(clip.title.trim(), `${where}: title 为空`).not.toBe("");
          // 时长徽标：GIF 没有进度条，访客先知道"这段要看多久"
          expect(clip.duration, `${where}: duration 应该形如 "25s"`).toMatch(/^\d+s$/);
          expect(
            clip.video || clip.gif,
            `${where}: 一段要么有 video，要么有 gif（没素材就别写这一段，整端未录用 note）`,
          ).toBeTruthy();
        }
      }
    }
  });

  it("pa 三端的段数：PC Web 4 / Mobile H5 2 / Mobile Native 1", () => {
    for (const [slug, expected] of Object.entries(EXPECTED_CLIP_COUNTS)) {
      const project = projects.find((item) => item.slug === slug);
      expect(project, `没有找到作品 ${slug}`).toBeTruthy();

      for (const [platform, count] of Object.entries(expected) as [Platform, number][]) {
        const demo = project?.demos.find((item) => item.platform === platform);
        expect(demo, `${slug} 缺少 ${platform} 端`).toBeTruthy();
        expect(demo?.clips.length, `${slug}/${platform} 的段数不对`).toBe(count);
      }
    }
  });

  // 这条是整份数据最有价值的一道闸：**声明了资产就必须真的存在**。
  // 静态页上「图片 404」不会报错，只会安静地留一块空白 —— 只有测试能提前发现文件名写错。
  it("声明了 video / gif / poster 的段，文件必须真实存在于 public/", () => {
    expect(existsSync(PUBLIC_DIR), `public/ 目录不存在：${PUBLIC_DIR}`).toBe(true);

    for (const project of projects) {
      for (const demo of project.demos) {
        for (const clip of demo.clips) {
          for (const field of ASSET_FIELDS) {
            const relativePath = clip[field];
            if (!relativePath) continue;

            const where = `${project.slug}/${demo.platform}/${clip.key} 的 ${field}`;
            // 只允许 public/ 下的相对路径：绝对路径与 ../ 都会部署到预期外的位置
            expect(relativePath.startsWith("/"), `${where} 不能用绝对路径：${relativePath}`).toBe(false);
            expect(relativePath.includes(".."), `${where} 不能跳出 public/：${relativePath}`).toBe(false);
            expect(existsSync(join(PUBLIC_DIR, relativePath)), `${where} 声明了 ${relativePath}，但文件不存在`).toBe(
              true,
            );
          }
        }
      }
    }
  });

  it("没有素材的端必须给 note（否则页面上是一块无说明的空白）", () => {
    for (const project of projects) {
      for (const demo of project.demos) {
        if (demo.clips.length > 0) continue;
        expect(demo.note?.trim(), `${project.slug}/${demo.platform} 端没有 clips，也没有 note`).toBeTruthy();
      }
    }
  });

  it("links.live 一旦填值就必须是 https 且不带尾斜杠", () => {
    for (const project of projects) {
      const live = project.links.live;
      if (!live) continue;
      expect(live.startsWith("https://"), `${project.slug}: live 必须是 https（http 会被浏览器标记为不安全）`).toBe(
        true,
      );
      expect(live.endsWith("/"), `${project.slug}: live 不要以 / 结尾`).toBe(false);
    }
  });
});
