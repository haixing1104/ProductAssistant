// 记住组织名（RN 版）用例 —— 它是"跨组织同名账号"能登进去的唯一凭据（登录时作为 `org_name` 消歧）。
//
// 三条口径必须钉住:
//   ① 只存**组织名**这一个非敏感字段（绝不出现密码/token）；
//   ② 空白输入不写（否则会把空串覆盖掉上次记住的值）；
//   ③ 存储层抛错**不能影响登录流程**（记住组织名是"便利"，不是"前置条件"）。
import { readRememberedOrg, rememberOrg, forgetOrg } from "../services/rememberOrg";

jest.mock("@react-native-async-storage/async-storage", () => ({
  __esModule: true,
  default: {
    getItem: jest.fn(),
    setItem: jest.fn(),
    removeItem: jest.fn(),
  },
}));

const storage = (require("@react-native-async-storage/async-storage") as { default: unknown })
  .default as { getItem: jest.Mock; setItem: jest.Mock; removeItem: jest.Mock };

/** 存储键名（与 H5/localStorage 同名：排查时一眼能对上）。 */
const ORG_KEY = "pa_mobile_org";

beforeEach(() => {
  jest.clearAllMocks();
});

describe("rememberOrg（组织名记忆）", () => {
  it("读：存过就读出来；没存过/存的是空值 → 空串（调用方退化为「没记住」）", async () => {
    storage.getItem.mockResolvedValueOnce("华强北选品部");
    expect(await readRememberedOrg()).toBe("华强北选品部");

    storage.getItem.mockResolvedValueOnce(null);
    expect(await readRememberedOrg()).toBe("");
  });

  it("写：只写组织名这一个键、且 trim 后落库（不存任何密码/token）", async () => {
    await rememberOrg("  华强北选品部  ");
    expect(storage.setItem).toHaveBeenCalledWith(ORG_KEY, "华强北选品部");
    // 键名固定，值只有一个参数 —— 结构上排除"顺手多存点东西"
    expect(storage.setItem.mock.calls[0]).toHaveLength(2);
  });

  it("写：空白输入直接跳过（不能把已记住的组织名覆盖成空）", async () => {
    await rememberOrg("   ");
    expect(storage.setItem).not.toHaveBeenCalled();
  });

  it("存储层抛错时全部静默降级（读给空串、写/清不冒泡到登录流程）", async () => {
    storage.getItem.mockRejectedValueOnce(new Error("storage down"));
    storage.setItem.mockRejectedValueOnce(new Error("storage down"));
    storage.removeItem.mockRejectedValueOnce(new Error("storage down"));

    await expect(readRememberedOrg()).resolves.toBe("");
    await expect(rememberOrg("华强北选品部")).resolves.toBeUndefined();
    await expect(forgetOrg()).resolves.toBeUndefined();
  });

  it("清：删除同一个键（换账号时不该把上一个人的组织名带出来）", async () => {
    await forgetOrg();
    expect(storage.removeItem).toHaveBeenCalledWith(ORG_KEY);
  });
});
