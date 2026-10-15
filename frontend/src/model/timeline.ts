/**
 * 时间线：把宿主近期行动与本页提交的控制合并为一条按时间倒序的记录。
 * 行动来自权威快照；控制记录目前只属于本标签页（宿主侧历史接入前的过渡）。
 */
import type { Entry } from "../client";
import type { Action, Snapshot } from "../contracts";
import { mark } from "../semantics";
import type { Mark } from "../semantics";
import { actionSubjects } from "./names";

export type TimelineFilter = "all" | "actions" | "controls" | "check";
export const FILTERS: readonly { id: TimelineFilter; label: string }[] = [
  { id: "all", label: "全部" }, { id: "actions", label: "她的行动" }, { id: "controls", label: "我的操作" }, { id: "check", label: "需要核对" },
];

export type TimelineItem =
  | { kind: "action"; id: string; at: number | null; title: string; status: Mark; needsCheck: boolean; action: Action }
  | { kind: "control"; id: string; at: number | null; title: string; status: Mark | null; needsCheck: boolean; entry: Entry };

/**
 * 为本页控制生成与行动身份不冲突的选择键。
 *
 * @param entry 保留原 request_id 的本页控制记录。
 * @returns 带 control: 前缀的稳定选择身份，不改变原业务请求身份。
 */
export function controlId(entry: Entry): string { return `control:${entry.request.request_id}`; }

/**
 * 根据实际调用对象生成行动标题。
 *
 * @param action 包含逐调用真实路径的行动记录。
 * @param snapshot 解析可配置插件实例名称的当前快照。
 * @returns 按首次出现顺序去重的对象标题；没有调用时明确说明。
 */
export function actionTitle(action: Action, snapshot: Snapshot | null): string {
  const subjects = actionSubjects(action.calls.map((call) => call.path), snapshot);
  return subjects.length === 0 ? "一次没有外部操作的行动" : `使用${subjects.join("、")}`;
}

/**
 * 合并宿主近期行动与本标签页控制，生成筛选后的时间线。
 *
 * @param snapshot 当前已校验快照；null 时仍保留本页控制。
 * @param entries 只属于当前标签页的原控制观察。
 * @param filter all、actions、controls 或 check，不修改原记录。
 * @returns 按已知时间倒序排列的结果；未知时间保留原相对顺序，不猜测为当前时间。
 */
export function timelineItems(snapshot: Snapshot | null, entries: readonly Entry[], filter: TimelineFilter): TimelineItem[] {
  const items: TimelineItem[] = [];
  for (const action of snapshot?.recent_actions ?? []) {
    items.push({
      kind: "action", id: action.action_id, at: action.created_at, title: actionTitle(action, snapshot),
      status: mark(action.state), needsCheck: action.calls.some((call) => call.state === "unknown"), action,
    });
  }
  for (const entry of entries) {
    items.push({
      kind: "control", id: controlId(entry), at: entry.result?.admitted_at ?? null, title: entry.title,
      status: entry.result ? mark(entry.result.action?.state ?? entry.result.activity_command?.status ?? entry.result.state)
        : entry.unavailable ? { state: "warning", label: "历史回执不可用" }
        : entry.rejected ? { state: "failed", label: "操作未被接受" } : null,
      needsCheck: entry.rejected || entry.unresolved || entry.unavailable, entry,
    });
  }
  // 缺少受理时间的记录保持原列表顺序；展示为未知，不能推断刚刚发起。
  items.sort((left, right) => {
    const l = left.at ?? Number.POSITIVE_INFINITY;
    const r = right.at ?? Number.POSITIVE_INFINITY;
    return l === r ? 0 : r > l ? 1 : -1;
  });
  return items.filter((item) => filter === "all" || (filter === "actions" && item.kind === "action")
    || (filter === "controls" && item.kind === "control") || (filter === "check" && item.needsCheck));
}
