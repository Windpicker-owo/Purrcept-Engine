/**
 * 把权威状态映射到 Cel 反馈语义，不从未知值猜测运行/成功。
 * 控制观察与业务实际结果分层；活动恢复资格只表达页面可用性，最终由 Host 核验。
 */
import type { AgentState } from "@purrcept/cel";
import type { Activity, ControlResult, Device, Json } from "./contracts";

export interface Mark { state: AgentState; label: string }
const STATES: Readonly<Record<string, Mark>> = {
  new: { state: "warning", label: "尚未启动" }, starting: { state: "waiting-external", label: "正在启动" },
  running: { state: "acting", label: "运行中" }, closing: { state: "waiting-external", label: "正在收尾" }, closed: { state: "paused", label: "已关闭" },
  online: { state: "completed", label: "在线" }, waiting: { state: "waiting-external", label: "等待外部接入" }, recovering: { state: "waiting-external", label: "恢复连接中" },
  exhausted: { state: "warning", label: "恢复次数已耗尽" }, failed: { state: "failed", label: "失败" }, stopping: { state: "waiting-external", label: "正在停止" },
  stopped: { state: "paused", label: "已停止" }, listening: { state: "perceiving", label: "正在收音" }, paused: { state: "paused", label: "已暂停" },
  pause_requested: { state: "waiting-external", label: "正在暂停，等待实际收尾" }, resume_requested: { state: "waiting-external", label: "正在恢复，等待确认" },
  end_requested: { state: "waiting-external", label: "正在结束，等待实际收尾" }, adjust_requested: { state: "waiting-external", label: "正在调整，等待确认" },
  ended: { state: "completed", label: "已结束" }, completed: { state: "completed", label: "已完成" },
  budget_exhausted: { state: "warning", label: "运行额度已耗尽" }, interrupted: { state: "warning", label: "中断，待核对" }, awaiting_decision: { state: "waiting-user", label: "等待主体核对" },
  confirmed: { state: "completed", label: "真实调用已确认" }, unknown: { state: "warning", label: "结果未知，需核对" }, not_started: { state: "paused", label: "未开始" },
  prepared: { state: "waiting-external", label: "已登记，尚未派发" }, started: { state: "waiting-external", label: "已派发，等待真实回执" },
  timed_out: { state: "warning", label: "等待超时；请查看逐调用结果" }, cancelled: { state: "paused", label: "等待已取消；请查看逐调用结果" },
  admitted: { state: "waiting-external", label: "已接管，等待业务返回" }, accepted: { state: "waiting-external", label: "已受理，等待驱动确认" }, rejected: { state: "failed", label: "已拒绝" },
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
  if (operation === "end") return activity.state === "end_requested" ? "正在结束，等待原命令确认。" : undefined;
  if (operation === "pause") return activity.state !== "running" ? "只有正在运行的活动可以请求暂停。" : undefined;
  if (activity.needs_reconciliation) return "重启或中断后的实际结果待主体核对，普通恢复不能绕过。";
  if (activity.remaining_seconds <= 0 || activity.state === "budget_exhausted") return "额度已耗尽；恢复不会追加运行额度。";
  if (activity.state !== "paused") return "只有已完成收尾的正常暂停活动可以恢复。";
  return undefined;
}
/** HTTP 202/finished 只描述接管层，真实效果在关联业务回执中。 */
export function controlMark(result: ControlResult): Mark {
  if (result.error !== null || result.state === "rejected") return { state: "failed", label: "控制被拒绝或返回错误" };
  if (result.state === "finished") return { state: "completed", label: "已取得业务返回；实际结果见下方" };
  return mark(result.state);
}
