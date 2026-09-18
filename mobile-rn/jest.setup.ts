// 测试环境公共桩（jest.setup.ts）。
//
// 与 mobile-h5/src/test/setup.ts 的定位一致：**只补测试环境缺的能力，不改变被测语义**。
// 移动端 RN 侧只有两类：① 原生模块必须打桩（jest 里没有原生运行时）；
// ② 与真机一致的“保底”mock（如 AsyncStorage 官方 mock，避免用例之间串数据）。
import mockAsyncStorage from "@react-native-async-storage/async-storage/jest/async-storage-mock";

// AsyncStorage：官方 mock（内存实现，afterEach 由用例自己 clear）
jest.mock("@react-native-async-storage/async-storage", () => mockAsyncStorage);

// react-native-screens：原生屏幕容器，测试里退化为空实现（否则渲染报缺失原生模块）
jest.mock("react-native-screens", () => {
  const actual = jest.requireActual("react-native-screens");
  return { ...actual, enableScreens: jest.fn() };
});
