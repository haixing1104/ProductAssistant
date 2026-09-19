import { existsSync } from "node:fs";
import { join, resolve } from "node:path";

import { projects } from "../data/projects";
import { PLATFORMS } from "../lib/asset";

// public/ 的绝对路径。
// 为什么不用 `import.meta.url`：本仓的 vitest 在 jsdom 环境下模块 URL **不是** file: 协议
// （实测抛 `TypeError: The URL must be of scheme file`），因此用 cwd —— `npm test` 与
// `../scripts/test-landing.sh` 都在 portfolio/ 下执行。断言里会把解析出的路径打出来，
// 万一在别的目录下跑，报错信息会直接告诉你它找了哪里。
const PUBLIC_DIR = resolve(process.cwd(), "public");

const ASSET_FIELDS = ["video", "gif", "poster"] as const;

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

  // 这条是整份数据最有价值的一道闸：**声明了资产就必须真的存在**。
  // 静态页上「视频 404」不会报错，只会安静地留一块空白 —— 只有测试能提前发现文件名写错。
  it("声明了 video / gif / poster 的端，文件必须真实存在于 public/", () => {
    expect(existsSync(PUBLIC_DIR), `public/ 目录不存在：${PUBLIC_DIR}`).toBe(true);

    for (const project of projects) {
      for (const demo of project.demos) {
        for (const field of ASSET_FIELDS) {
          const relativePath = demo[field];
          if (!relativePath) continue;

          const where = `${project.slug}/${demo.platform} 的 ${field}`;
          // 只允许 public/ 下的相对路径：绝对路径与 ../ 都会部署到预期外的位置
          expect(relativePath.startsWith("/"), `${where} 不能用绝对路径：${relativePath}`).toBe(false);
          expect(relativePath.includes(".."), `${where} 不能跳出 public/：${relativePath}`).toBe(false);
          expect(existsSync(join(PUBLIC_DIR, relativePath)), `${where} 声明了 ${relativePath}，但文件不存在`).toBe(
            true,
          );
        }
      }
    }
  });

  it("没有视频/动图的端必须给 note（否则页面上是一块无说明的空白）", () => {
    for (const project of projects) {
      for (const demo of project.demos) {
        if (demo.video || demo.gif) continue;
        expect(
          demo.note?.trim(),
          `${project.slug}/${demo.platform} 既没有 video/gif，也没有 note`,
        ).toBeTruthy();
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
