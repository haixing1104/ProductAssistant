// 组织名记忆（H5 版，localStorage）—— 移动端专属纯函数。
//
// 为什么单独守护: 记住组织名是手机上唯一的"免手打"便利，而隐私模式/禁用 storage 时**必须优雅退化**
//（抛错会让整个登录页白屏）。
// 注意: SSE 阶段行（`formatEvent`）与展示工具（`mobileFormat`）的用例已随文件上移到共享层
//（`frontend/src/__tests__/streamLabels.test.ts` / `mobileFormat.test.ts`）—— 那两个文件是 H5 与 RN 共用的。
import { describe, expect, it } from "vitest";

import { forgetOrg, readRememberedOrg, rememberOrg } from "../services/rememberOrg";

/** 最小 Storage 替身（jsdom 有 localStorage，但这里要覆盖"抛错"与"不存在"两种真实场景）。 */
function fakeStorage(): Storage {
  const map = new Map<string, string>();
  return {
    get length() {
      return map.size;
    },
    clear: () => map.clear(),
    getItem: (k: string) => map.get(k) ?? null,
    key: (i: number) => Array.from(map.keys())[i] ?? null,
    removeItem: (k: string) => void map.delete(k),
    setItem: (k: string, v: string) => void map.set(k, v),
  } as Storage;
}

describe("rememberOrg（记住组织名）", () => {
  it("记住后能读回；空白值不写入", () => {
    const s = fakeStorage();
    rememberOrg("  示例科技  ", s);
    expect(readRememberedOrg(s)).toBe("示例科技");
    rememberOrg("   ", s);
    expect(readRememberedOrg(s)).toBe("示例科技");
  });

  it("清除后读回空串", () => {
    const s = fakeStorage();
    rememberOrg("示例科技", s);
    forgetOrg(s);
    expect(readRememberedOrg(s)).toBe("");
  });

  it("storage 不可用（隐私模式/抛错）时静默退化，不抛错", () => {
    const broken = {
      getItem: () => {
        throw new Error("SecurityError");
      },
      setItem: () => {
        throw new Error("SecurityError");
      },
      removeItem: () => {
        throw new Error("SecurityError");
      },
    } as unknown as Storage;
    expect(readRememberedOrg(broken)).toBe("");
    expect(() => rememberOrg("示例科技", broken)).not.toThrow();
    expect(() => forgetOrg(broken)).not.toThrow();
    // null（无 window）同样不抛
    expect(readRememberedOrg(null)).toBe("");
    expect(() => rememberOrg("x", null)).not.toThrow();
  });
});
