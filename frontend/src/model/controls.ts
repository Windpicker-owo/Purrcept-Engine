/**
 * 控制资格：全局写入条件与同一操作的合并。只决定按钮是否可用及原因，
 * 最终是否接管由宿主核验；页面不据此改写设备或活动状态。
 */
import { awaiting } from "../client";
import type { PanelState } from "../client";

/**
 * 读取全局控制入口当前不可用的原因。
 *
 * @param state 页面观察者持有的连接、宿主阶段与权威写入状态。
 * @returns 阻止新控制的原因；undefined 表示全局条件允许。
 */
export function blocked(state: PanelState): string | undefined {
  if (!state.online) return "与本机服务的连接中断，恢复后才能操作。";
  if (!state.snapshot?.authority_available) return "数据无法保存，暂时不能发起新操作。";
  if (state.snapshot.phase !== "running") return "Engine 尚未就绪或正在关闭。";
  if (!state.snapshot.health.can_process) return "主体组件发生故障，新的操作已暂停。";
  return undefined;
}
/**
 * 合并同一目标下仍在途的相同控制意图。
 *
 * @param state 当前共享页面观察状态。
 * @param target 目标活动或设备的稳定身份。
 * @param operation 明确控制操作名，停止和结束与其他操作分别判断。
 * @returns 全局或同操作阻止原因；undefined 表示页面可提交新意图。
 */
export function targetReason(state: PanelState, target: string, operation: string): string | undefined {
  return blocked(state) ?? (state.entries.some((entry) => entry.request.instance_id === state.snapshot?.instance_id && entry.request.target === target && entry.request.operation === operation && awaiting(entry)) ? "同样的操作还在进行，请等它完成。" : undefined);
}
