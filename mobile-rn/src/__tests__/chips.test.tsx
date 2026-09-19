// Chips（单选芯片）用例 —— 只钉两件事，但第一件是**真机上肉眼可见**的布局事故：
//
// ① `scrollable` 分支用的是**横向 `ScrollView`**，而 RN 的 ScrollView 自带 `baseHorizontal`
//    （`{ flexGrow: 1, flexShrink: 1 }`，见 `Libraries/Components/ScrollView/ScrollView.js:1887`）。
//    不显式压掉它，这条"筛选行"会在**竖直方向**长大去吃空白 —— 下面的列表被推到屏幕中段，
//    看起来就是"只剩 1 条数据时垂直居中、上下各留一块空白"（2026-09 真机截图定位到它）。
// ② 单选交互：点一下就回调，且带出该选项的 `value`（筛选参数与表单取值共用同一套语义）。
import { fireEvent, render } from "@testing-library/react-native";
import { StyleSheet } from "react-native";

import Chips from "../ui/Chips";

/** 列表页的状态筛选行（本事故的现场）。 */
function renderChips(scrollable: boolean) {
  return render(
    <Chips
      options={[
        { value: "", label: "全部" },
        { value: "draft", label: "草稿" },
      ]}
      value=""
      onChange={jest.fn()}
      scrollable={scrollable}
      testID="pa-chips"
    />,
  );
}

describe("Chips", () => {
  it("可滚动分支必须压掉 flexGrow/flexShrink（否则横向行会纵向吃空白、把列表推到中段）", async () => {
    const view = await renderChips(true);
    const style = StyleSheet.flatten(view.getByTestId("pa-chips").props.style);
    expect(style.flexGrow).toBe(0);
    expect(style.flexShrink).toBe(0);
  });

  it("点选后回调带出该选项的 value；选中态由传入的 value 决定（受控）", async () => {
    const onChange = jest.fn();
    const options = [
      { value: "", label: "全部" },
      { value: "draft", label: "草稿" },
    ];
    const view = await render(
      <Chips options={options} value="" onChange={onChange} scrollable testID="pa-chips" />,
    );
    await fireEvent.press(view.getByText("草稿"));
    expect(onChange).toHaveBeenCalledWith("draft");

    // 受控语义：选中态来自父级传回的 value，而不是组件内部 state
    const selected = await render(<Chips options={options} value="draft" onChange={jest.fn()} testID="pa-chips-on" />);
    expect(selected.getByText("草稿").parent?.props.accessibilityState).toEqual({ selected: true });
    expect(selected.getByText("全部").parent?.props.accessibilityState).toEqual({ selected: false });
  });
});
