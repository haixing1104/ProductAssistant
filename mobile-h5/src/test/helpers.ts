// 测试工具：等待"弹层真的可交互"。
//
// 为什么需要（antd-mobile + jsdom 的既有现象）: Popup / Dialog / ActionSheet 用 react-spring 做入场动画，
// 动画期间会在内容容器上写 `pointer-events: none`（`popup.js`: `percent.to(v => v === 0 ? 'unset' : 'none')`）。
// jsdom 里这个 spring 要若干帧才收敛，直接点击/输入会报
// "Unable to perform pointer interaction as the element has pointer-events: none"。
// 等待点是**语义正确**的：真人在手机上也是等弹层滑出来再操作。
import userEvent from "@testing-library/user-event";
import { waitFor } from "@testing-library/react";
import { expect } from "vitest";

/** 等元素可点击（pointer-events 不再是 none）。 */
export async function waitClickable(el: HTMLElement, timeout = 3000): Promise<HTMLElement> {
  await waitFor(() => expect(el).not.toHaveStyle({ pointerEvents: "none" }), { timeout });
  return el;
}

/** 点击：先等弹层动画收敛。 */
export async function clickWhenReady(el: HTMLElement): Promise<void> {
  const user = userEvent.setup();
  await waitClickable(el);
  await user.click(el);
}

/** 输入：先等弹层动画收敛。 */
export async function typeWhenReady(el: HTMLElement, text: string): Promise<void> {
  const user = userEvent.setup();
  await waitClickable(el);
  await user.type(el, text);
}
