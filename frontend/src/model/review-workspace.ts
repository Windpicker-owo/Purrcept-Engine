/**
 * 审查工作区的页面内记忆：筛选、列表阅读位置及按宿主/目标隔离的草稿。
 * App 创建一次并持有到页面卸载；不写浏览器存储、不提交评价，也不缓存权威材料。
 */
export interface ReviewDraft { rating: "good" | "bad" | null; comment: string }
/** 所有字段只由用户导航或输入更新；宿主材料仍由 client 查询和校验。 */
export interface ReviewWorkspace {
  kind: "action" | "turn" | "call";
  cursor: string | null;
  scrollY: number;
  drafts: Map<string, ReviewDraft>;
}
/** 为一个 App 生命周期建立 UI 状态，不把草稿跨宿主复用。 */
export function createReviewWorkspace(): ReviewWorkspace {
  return { kind: "action", cursor: null, scrollY: 0, drafts: new Map() };
}
