// E2E 冒烟（默认 skip）：需真实全栈 —— backend-api 跑在 8000（真实 PG/Redis）+ 前端 5173。
// 启用：PA_E2E=1 npx playwright test
// 前置：先在 backend 侧注册一个组织（或在用例里用 /auth/register 现开一个）。
import { expect, test } from "@playwright/test";

const RUN_E2E = process.env.PA_E2E === "1";

test.skip(!RUN_E2E, "登录并进入商品列表（需 backend + 前端都在跑，且 PA_E2E=1）", async ({ page }) => {
  await page.goto("/login");
  await expect(page.getByText("ProductAssistant")).toBeVisible();
  await page.getByPlaceholder("用户名").fill(process.env.PA_E2E_USER ?? "admin");
  await page.getByPlaceholder("密码（至少 8 位）").fill(process.env.PA_E2E_PASSWORD ?? "ProductAssistant@123");
  await page.getByRole("button", { name: "登录" }).click();
  await expect(page.getByText("商品管理")).toBeVisible();
});
