/**
 * 把权威状态映射到 Cel 反馈语义与用户文案，不从未知值猜测运行/成功。
 * 控制观察与业务实际结果分层；活动恢复资格只表达页面可用性，最终由 Host 核验。
 * 状态词遵循 Design：运行中 / 等待中 / 等你确认 / 已暂停 / 已完成 / 失败；
 * 原始状态值保留给 Inspector 的技术细节，不在这里改写。
 */
import type { AgentState } from "@purrcept/cel";
import type { Activity, ControlResult, Device, Json } from "./contracts";

export interface Mark { state: AgentState; label: string }
const STATES: Readonly<Record<string, Mark>> = {
  new: { state: "warning", label: "尚未启动" }, starting: { state: "waiting-external", label: "正在启动" },
  running: { state: "acting", label: "运行中" }, closing: { state: "waiting-external", label: "正在收尾" }, closed: { state: "paused", label: "已关闭" },
  online: { state: "completed", label: "在线" }, waiting: { state: "waiting-external", label: "等待对方连入" }, recovering: { state: "waiting-external", label: "正在重新连接" },
  exhausted: { state: "warning", label: "多次重连未成功" }, failed: { state: "failed", label: "失败" }, stopping: { state: "waiting-external", label: "正在停止" },
  stopped: { state: "paused", label: "已停止" }, listening: { state: "perceiving", label: "正在听" }, paused: { state: "paused", label: "已暂停" },
  pause_requested: { state: "waiting-external", label: "正在暂停，等待收尾" }, resume_requested: { state: "waiting-external", label: "正在恢复" },
  end_requested: { state: "waiting-external", label: "正在结束，等待收尾" }, adjust_requested: { state: "waiting-external", label: "正在调整" },
  ended: { state: "completed", label: "已结束" }, completed: { state: "completed", label: "已完成" },
  budget_exhausted: { state: "warning", label: "运行额度已用完" }, interrupted: { state: "warning", label: "中断，等她核对结果" },
  // 核对由主体完成，不是用户决定；waiting-user 只留给真正需要用户处理的事项。
  awaiting_decision: { state: "waiting-external", label: "等她核对结果" },
  confirmed: { state: "completed", label: "已完成" }, unknown: { state: "warning", label: "不确定是否已执行" }, not_started: { state: "paused", label: "未执行" },
  prepared: { state: "waiting-external", label: "准备中" }, started: { state: "acting", label: "执行中" },
  timed_out: { state: "warning", label: "等待超时，部分结果需核对" }, cancelled: { state: "paused", label: "已取消，部分结果需核对" },
  admitted: { state: "waiting-external", label: "已收到，正在执行" }, accepted: { state: "waiting-external", label: "已收到，等待设备确认" }, rejected: { state: "failed", label: "未被接受" },
};

/** 已认识的状态使用同一形状/颜色；未知值保持可见。 */
export function mark(value: string | undefined): Mark {
  return value === undefined ? { state: "warning", label: "状态未提供" } : STATES[value] ?? { state: "warning", label: `未知状态：${value}` };
}
/** 模式、错误与可选状态字段只从已校验 JSON 读取。 */
export function stringField(value: Json | undefined): string | undefined { return typeof value === "string" ? value : undefined; }
/** 没有实际连接字段时不能显示离线或在线。 */
export function deviceMark(device: Device): Mark {
  const state = device.state;
  if (typeof state.state === "string") return mark(state.state);
  if (typeof state.connected === "boolean") return state.connected ? { state: "completed", label: "已连接" } : { state: "paused", label: "未连接" };
  if (typeof state.ready === "boolean") return state.ready ? { state: "completed", label: "已就绪" } : { state: "warning", label: "尚未就绪" };
  return mark(undefined);
}
/** 正常暂停且余额可用才允许普通恢复；不会隐式追加或绕过核对。 */
export function activityReason(activity: Activity, operation: "pause" | "resume" | "end"): string | undefined {
  if (["ended", "completed"].includes(activity.state)) return "活动已经结束，不能重新启用。";
  if (operation === "end") return activity.state === "end_requested" ? "正在结束，等待收尾完成。" : undefined;
  if (operation === "pause") return activity.state !== "running" ? "只有正在运行的活动可以暂停。" : undefined;
  if (activity.needs_reconciliation) return "中断后的结果需要她先核对，暂时不能直接恢复。";
  if (activity.remaining_seconds <= 0 || activity.state === "budget_exhausted") return "运行额度已用完；恢复不会追加额度。";
  if (activity.state !== "paused") return "只有已经停稳的暂停活动可以恢复。";
  return undefined;
}
/** HTTP 202/finished 只描述接管层，真实效果在关联业务回执中。 */
export function controlMark(result: ControlResult): Mark {
  if (result.error !== null || result.state === "rejected") return { state: "failed", label: "操作未被接受" };
  if (result.state === "finished") return { state: "completed", label: "已处理；实际结果见下方" };
  return mark(result.state);
}
