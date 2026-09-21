import { PROJECT_SETS, projects, projectsFor } from "../data/projects";
import { projectsEn } from "../data/projects.en";

// 作品数据的英文版（`data/projects.en.ts`）的**对齐门禁**。
//
// 结构对齐的编译期那一半由 `Project[]` 类型保证（字段少一个就过不了 tsc）；
// 这里补 tsc 管不了的那一半：数量 / 顺序 / 素材路径 / 内容是否真的翻译了。

/** 中日韩字符 + 全角标点（与 `strings.test.ts` 同口径）。 */
const CJK = /[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]/;

describe("作品数据的英文版（src/data/projects.en.ts）", () => {
  it("与中文版逐条对应：数量 / slug / 端顺序 / 段 key / 素材路径 / links / stack 全一致", () => {
    expect(projectsEn).toHaveLength(projects.length);
    expect(projectsEn.map((project) => project.slug)).toEqual(projects.map((project) => project.slug));

    projects.forEach((zh, index) => {
      const en = projectsEn[index];
      const where = `${zh.slug}[${index}]`;

      // 状态是枚举不是文案 → 两种语言必须一致（否则切语言会变成"未上线"）
      expect(en.status, `${where}: status 不一致`).toBe(zh.status);
      // 入口地址与语言无关 → 两种语言必须逐字一致（否则切语言会指向另一套系统）
      expect(en.links, `${where}: links 不一致`).toEqual(zh.links);
      // 技术栈本就是英文 → 不翻译，逐项一致
      expect(en.stack, `${where}: stack 不一致`).toEqual(zh.stack);

      // 结构：端的顺序、端名、舞台比例、段顺序、段 key 都必须一致 ——
      // 端名（PC Web / Mobile H5 / …）也不翻译：否则切语言时端 Tab 改名，看起来像换了一个端
      expect(en.demos.map((demo) => demo.platform), `${where}: 端顺序不一致`).toEqual(
        zh.demos.map((demo) => demo.platform),
      );
      expect(en.demos.map((demo) => demo.label), `${where}: 端名不一致`).toEqual(
        zh.demos.map((demo) => demo.label),
      );
      expect(en.demos.map((demo) => demo.aspect), `${where}: aspect 不一致`).toEqual(
        zh.demos.map((demo) => demo.aspect),
      );
      expect(en.demos.map((demo) => demo.clips.map((clip) => clip.key)), `${where}: 段 key 不一致`).toEqual(
        zh.demos.map((demo) => demo.clips.map((clip) => clip.key)),
      );
      // 素材路径一致 = 两种语言指向**同一批** `public/` 文件：不重复存图，也不会漏放一套
      expect(en.demos.map((demo) => demo.clips.map((clip) => [clip.gif, clip.video, clip.poster]))).toEqual(
        zh.demos.map((demo) => demo.clips.map((clip) => [clip.gif, clip.video, clip.poster])),
      );
      // 条数一致：highlights 少一条不会报错，只会少一个卖点
      expect(en.highlights, `${where}: highlights 条数不一致`).toHaveLength(zh.highlights.length);
      expect(en.demos.map((demo) => demo.clips.length), `${where}: 段数不一致`).toEqual(
        zh.demos.map((demo) => demo.clips.length),
      );
    });
  });

  it("英文版展示字段填齐（空白页比报错更难发现）", () => {
    for (const project of projectsEn) {
      expect(project.name.trim(), `${project.slug}: name 为空`).not.toBe("");
      expect(project.tagline.trim(), `${project.slug}: tagline 为空`).not.toBe("");
      expect(project.summary.trim(), `${project.slug}: summary 为空`).not.toBe("");
      expect(project.period.trim(), `${project.slug}: period 为空`).not.toBe("");
      expect(project.highlights.length, `${project.slug}: highlights 为空`).toBeGreaterThan(0);
    }
  });

  it("英文版不得残留中文（含全角标点）—— 漏译的机械门禁", () => {
    const texts: Array<[string, string]> = [];

    for (const project of projectsEn) {
      texts.push(
        [`${project.slug}.name`, project.name],
        [`${project.slug}.tagline`, project.tagline],
        [`${project.slug}.summary`, project.summary],
        [`${project.slug}.period`, project.period],
      );
      project.highlights.forEach((highlight, index) =>
        texts.push([`${project.slug}.highlights[${index}]`, highlight]),
      );

      for (const demo of project.demos) {
        const where = `${project.slug}/${demo.platform}`;
        if (demo.note) texts.push([`${where}.note`, demo.note]);
        for (const clip of demo.clips) {
          texts.push([`${where}/${clip.key}.title`, clip.title]);
          if (clip.note) texts.push([`${where}/${clip.key}.note`, clip.note]);
        }
      }
    }

    for (const [path, text] of texts) {
      expect(CJK.test(text), `${path} 还有中文未翻译 → ${text}`).toBe(false);
    }
  });

  it("两种语言的文案确实不同（把中文复制一份当英文，页面切了没反应）", () => {
    projects.forEach((zh, index) => {
      expect(projectsEn[index].tagline, `${zh.slug}: 英文 tagline 与中文相同`).not.toBe(zh.tagline);
      expect(projectsEn[index].summary, `${zh.slug}: 英文 summary 与中文相同`).not.toBe(zh.summary);
    });
  });

  it("projectsFor：按语言取；语言值异常时回退中文（页面不空白）", () => {
    expect(projectsFor("zh")).toBe(projects);
    expect(projectsFor("en")).toBe(projectsEn);
    expect(PROJECT_SETS.zh).toBe(projects);
    expect(PROJECT_SETS.en).toBe(projectsEn);
    // @ts-expect-error 故意传非法语言值：运行期（localStorage 被手改）真的会走到这条兜底
    expect(projectsFor("fr")).toBe(projects);
  });
});
