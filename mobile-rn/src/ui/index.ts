// UI 基座出口（自研薄 UI；**不引任何 UI 依赖** —— 理由见 mobile-rn/README 的 ADR）。
//
// 组件名刻意与 antd-mobile 对齐（Tag / Card / ListSection / NoticeBar / Tabs / Sheet / ActionSheet /
// Toast / Dialog），这样页面代码在 H5 与 RN 之间的阅读成本最低，评审时也能逐行对应。
export { default as Button } from "./Button";
export type { ButtonFill, ButtonSize, ButtonVariant } from "./Button";
export { default as Card } from "./Card";
export { default as Chips } from "./Chips";
export type { ChipOption } from "./Chips";
export { LabeledInput, LabeledTextArea } from "./Field";
export { Dialog, FeedbackHost, Toast, resetFeedbackListeners } from "./feedback";
export type { DialogOptions, ToastOptions } from "./feedback";
export { ImageThumbs, ImageViewer } from "./ImageViewer";
export { EmptyState, ListRow, ListSection, Loading, PreWrapText } from "./List";
export { NavBar, NoticeBar } from "./NavBar";
export { default as Screen, ACTION_BAR_HEIGHT } from "./Screen";
export { ActionSheet, Sheet } from "./Sheet";
export type { SheetAction } from "./Sheet";
export { default as Tag } from "./Tag";
export { default as Tabs } from "./Tabs";
export type { TabItem } from "./Tabs";
export {
  colors,
  font,
  radius,
  space,
  TAB_BAR_HEIGHT,
  tagFilled,
  tagPalette,
  TOUCH_TARGET,
} from "./theme";
export type { TagPalette } from "./theme";
