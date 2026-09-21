import ContactFooter from "./components/ContactFooter";
import Hero from "./components/Hero";
import { LanguageProvider, useLang } from "./components/LanguageProvider";
import ProjectCard from "./components/ProjectCard";
import { projectsFor } from "./data/projects";

// 单页装配：Hero → 作品列表（数据驱动）→ 联系我。
//
// 为什么 v1 不引路由：只有一个作品时，一页 + 锚点是最省心的形态；
// 等作品多到需要「每个项目一个详情页」时再加 react-router（届时 `/p/:slug`），
// 现在的数据结构（`slug`）已经为那天准备好了。
//
// `LanguageProvider` 放在**这里**（而不是 `main.tsx`）：`App` 就是应用根，
// 于是任何 `render(<App />)` 的用例天然拿到可用的语言切换（`main.tsx` 不用改）；
// 组件级用例（如直接渲染 `ProjectCard`）则走 Provider 的默认值 = 中文。
export default function App() {
  return (
    <LanguageProvider>
      <Page />
    </LanguageProvider>
  );
}

/** 消费语言上下文的这一层：文案与作品数据都按当前语言取。 */
function Page() {
  const { lang } = useLang();

  return (
    <>
      <Hero />
      <main id="projects" className="mx-auto max-w-5xl px-6 py-16 sm:py-20">
        <div className="mt-8 flex flex-col gap-8">
          {projectsFor(lang).map((project) => (
            <ProjectCard key={project.slug} project={project} />
          ))}
        </div>
      </main>
      <ContactFooter />
    </>
  );
}
