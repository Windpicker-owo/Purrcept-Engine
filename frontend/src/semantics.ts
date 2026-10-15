/**
 * 把权威状态映射到 Cel 反馈语义与用户文案，不从未知值猜测运行/成功。
 * 控制观察与业务实际结果分层；活动恢复资格只表达页面可用性，最终由 Host 核验。
 * 状态词遵循 Design：运行中 / 等待中 / 等你确认 / 已暂停 / 已完成 / 失败；
 * 原始状态值保留给 Inspector 的技术细节，不在这里改写。
 */
import type { AgentState } from "@purrcept/cel";
import type { Activity, ControlResult, Device, Json } from "./contracts";

/** 从原始业务事实推导的 Cel 形状与可读状态词，不反向修改业务状态。 */
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

/**
 * 将宿主原始状态映射为统一标记与用户文案。
 *
 * @param value 实际状态字符串；undefined 表示宿主未提供。
 * @returns 已知状态的 Cel 表示，或明确显示未知原值的 warning 标记。
 */
export function mark(value: string | undefined): Mark {
  return value === undefined ? { state: "warning", label: "状态未提供" } : STATES[value] ?? { state: "warning", label: `未知状态：${value}` };
}
/**
 * 读取已校验 JSON 中的可选文本字段。
 *
 * @param value 插件真实状态字段，可能缺失或属于其他 JSON 种类。
 * @returns 原字符串；其他值返回 undefined，不转换对象或数字。
 */
export function stringField(value: Json | undefined): string | undefined { return typeof value === "string" ? value : undefined; }
/**
 * 从设备实际提供的字段推导当前状态标记。
 *
 * @param device 宿主快照中的已装配设备及其原生状态。
 * @returns 依次采用 state、connected、ready；都未提供时明确显示状态未知。
 */
export function deviceMark(device: Device): Mark {
  const state = device.state;
  if (typeof state.state === "string") return mark(state.state);
  if (typeof state.connected === "boolean") return state.connected ? { state: "completed", label: "已连接" } : { state: "paused", label: "未连接" };
  if (typeof state.ready === "boolean") return state.ready ? { state: "completed", label: "已就绪" } : { state: "warning", label: "尚未就绪" };
  return mark(undefined);
}
/**
 * 检查当前页面能否发起明确活动控制。
 *
 * @param activity 宿主实际活动状态、剩余预算与恢复核对要求。
 * @param operation pause、resume 或 end；恢复不增加额度。
 * @returns 不可操作的具体原因；undefined 表示页面条件允许，宿主仍会再次核验。
 */
export function activityReason(activity: Activity, operation: "pause" | "resume" | "end"): string | undefined {
  if (["ended", "completed"].includes(activity.state)) return "活动已经结束，不能重新启用。";
  if (operation === "end") return activity.state === "end_requested" ? "正在结束，等待收尾完成。" : undefined;
  if (operation === "pause") return activity.state !== "running" ? "只有正在运行的活动可以暂停。" : undefined;
  if (activity.needs_reconciliation) return "中断后的结果需要她先核对，暂时不能直接恢复。";
  if (activity.remaining_seconds <= 0 || activity.state === "budget_exhausted") return "运行额度已用完；恢复不会追加额度。";
  if (activity.state !== "paused") return "只有已经停稳的暂停活动可以恢复。";
  return undefined;
}
/**
 * 展示控制接管层的结果，不替代真实业务回执。
 *
 * @param result 宿主实际返回的控制记录。
 * @returns 拒绝、处理完成或仍在等待的控制标记；关联行动与命令须另行展示。
 */
export function controlMark(result: ControlResult): Mark {
  if (result.error !== null || result.state === "rejected") return { state: "failed", label: "操作未被接受" };
  if (result.state === "finished") return { state: "completed", label: "已处理；实际结果见下方" };
  return mark(result.state);
}
