import ContactFooter from "./components/ContactFooter";
import Hero from "./components/Hero";
import ProjectCard from "./components/ProjectCard";
import { projects } from "./data/projects";

// 单页装配：Hero → 作品列表（数据驱动）→ 联系我。
//
// 为什么 v1 不引路由：只有一个作品时，一页 + 锚点是最省心的形态；
// 等作品多到需要「每个项目一个详情页」时再加 react-router（届时 `/p/:slug`），
// 现在的数据结构（`slug`）已经为那天准备好了。
export default function App() {
  return (
    <>
      <Hero />
      <main id="projects" className="mx-auto max-w-5xl px-6 py-16 sm:py-20">

        <div className="mt-8 flex flex-col gap-8">
          {projects.map((project) => (
            <ProjectCard key={project.slug} project={project} />
          ))}
        </div>
      </main>
      <ContactFooter />
    </>
  );
}
