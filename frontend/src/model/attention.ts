/**
 * 待处理事项：把散落在快照与本页控制记录中的 until-resolved 条件汇成唯一列表。
 * 首页、导航徽标和待处理页共用同一结果；条件从快照中消失即自然退出，不靠 Toast。
 * 全局断线属于 system 范围，由顶栏横幅表达，不进入列表。
 */
import type { AgentState } from "@purrcept/cel";
import type { Entry } from "../client";
import type { Snapshot } from "../contracts";
import { deviceMark } from "../semantics";
import { actionSubjects, deviceName } from "./names";
import type { Route } from "./routes";

export interface AttentionItem {
  /** 稳定身份，用作列表 key 与动画身份。 */
  id: string;
  state: AgentState;
  /** 发生了什么。 */
  title: string;
  /** 影响了什么。 */
  impact: string;
  /** 现在能做什么；按钮文字使用动词短语。 */
  next: string;
  route: Route;
  /** 需要用户本人到外部核对或操作，而不是等待她或设备自行完成。 */
  needsUser: boolean;
}

/** 顺序：存储 → 设备 → 结果未知 → 活动 → 容量 → 本页控制。 */
export function attentionItems(snapshot: Snapshot | null, entries: readonly Entry[]): AttentionItem[] {
  if (snapshot === null) return [];
  const items: AttentionItem[] = [];
  if (!snapshot.authority_available) {
    items.push({
      id: "storage", state: "failed", title: "数据无法保存",
      impact: "新的控制和行动已暂停，已有记录不受影响。", next: "查看运行状况",
      route: { page: "system", id: null }, needsUser: true,
    });
  }
  for (const device of snapshot.devices) {
    const status = deviceMark(device);
    const error = typeof device.state.error === "string" && device.state.error !== "" ? device.state.error : null;
    if (status.state !== "failed" && device.state.state !== "exhausted" && error === null) continue;
    items.push({
      id: `device:${device.instance_id}`, state: status.state === "failed" ? "failed" : "warning",
      title: `${deviceName(device)}：${status.label}`,
      impact: error ?? "这个设备暂时不能正常工作。", next: "查看设备",
      route: { page: "devices", id: device.instance_id }, needsUser: true,
    });
  }
  for (const action of snapshot.recent_actions) {
    const unknown = action.calls.filter((call) => call.state === "unknown");
    if (unknown.length === 0) continue;
    const subjects = actionSubjects(unknown.map((call) => call.path), snapshot).join("、");
    items.push({
      id: `action:${action.action_id}`, state: "warning", title: `不确定 ${subjects} 的操作是否已执行`,
      impact: `${unknown.length} 项操作没有取得确认，不会自动重做。`, next: "查看详情并到对应应用核对",
      route: { page: "timeline", id: action.action_id }, needsUser: true,
    });
  }
  for (const activity of snapshot.activities) {
    if (activity.needs_reconciliation) {
      items.push({
        id: `activity:${activity.activity_id}:reconcile`, state: "waiting-external", title: `活动“${activity.goal}”中断后待核对`,
        impact: "她会先核对上次的实际结果，再决定是否继续。", next: "查看活动",
        route: { page: "activities", id: activity.activity_id }, needsUser: false,
      });
    } else if (activity.state === "budget_exhausted") {
      items.push({
        id: `activity:${activity.activity_id}:budget`, state: "warning", title: `活动“${activity.goal}”的运行额度已用完`,
        impact: "活动不会再推进新步骤，恢复也不会追加额度。", next: "查看活动",
        route: { page: "activities", id: activity.activity_id }, needsUser: true,
      });
    }
  }
  const count = snapshot.pending_inputs.reduce((sum, item) => sum + item.count, 0);
  const bytes = snapshot.pending_inputs.reduce((sum, item) => sum + item.logical_bytes, 0);
  if (count >= snapshot.pending_limits.count || bytes >= snapshot.pending_limits.logical_bytes) {
    items.push({
      id: "capacity", state: "warning", title: "待处理消息已经堆满",
      impact: "新消息可能被拒收或延后，已收到的消息不会被挤掉。", next: "查看运行状况",
      route: { page: "system", id: null }, needsUser: false,
    });
  }
  if (snapshot.pending_resources.length > 0) {
    items.push({
      id: "resources", state: "warning", title: "仍有资源在释放",
      impact: snapshot.pending_resources.join("、"), next: "查看运行状况",
      route: { page: "system", id: null }, needsUser: false,
    });
  }
  for (const entry of entries) {
    if (entry.request.instance_id !== snapshot.instance_id) continue;
    if (entry.rejected || (entry.unresolved && !entry.sending)) {
      items.push({
        id: `control:${entry.request.request_id}`, state: entry.rejected ? "failed" : "warning",
        title: entry.rejected ? `“${entry.title}”未被接受` : `不确定“${entry.title}”是否已送达`,
        impact: entry.error ?? "页面没有取得宿主的回应。", next: "查看操作记录",
        route: { page: "timeline", id: `control:${entry.request.request_id}` }, needsUser: true,
      });
    }
  }
  return items;
}
