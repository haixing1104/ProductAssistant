import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import App from "./App";
// 全局样式入口（Tailwind v4）：token 定义与语义类都在这里，见 src/styles/app.css 顶部注释
import "./styles/app.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
