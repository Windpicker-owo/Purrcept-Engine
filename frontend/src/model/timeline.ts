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

/** 选中身份：行动用 action_id，控制加 control: 前缀，避免两类身份碰撞。 */
export function controlId(entry: Entry): string { return `control:${entry.request.request_id}`; }

/** 行动的人话标题：涉及哪些对象，没有调用时如实说明。 */
export function actionTitle(action: Action, snapshot: Snapshot | null): string {
  const subjects = actionSubjects(action.calls.map((call) => call.path), snapshot);
  return subjects.length === 0 ? "一次没有外部操作的行动" : `使用${subjects.join("、")}`;
}

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
      status: entry.result ? mark(entry.result.action?.state ?? entry.result.activity_command?.status ?? entry.result.state) : null,
      needsCheck: entry.rejected || entry.unresolved, entry,
    });
  }
  // 尚未取得受理时间的控制是刚刚发起的，排在最前。
  items.sort((left, right) => {
    const l = left.at ?? Number.POSITIVE_INFINITY;
    const r = right.at ?? Number.POSITIVE_INFINITY;
    return l === r ? 0 : r > l ? 1 : -1;
  });
  return items.filter((item) => filter === "all" || (filter === "actions" && item.kind === "action")
    || (filter === "controls" && item.kind === "control") || (filter === "check" && item.needsCheck));
}
