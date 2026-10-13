/**
 * 控制资格：全局写入条件与同一操作的合并。只决定按钮是否可用及原因，
 * 最终是否接管由宿主核验；页面不据此改写设备或活动状态。
 */
import { awaiting } from "../client";
import type { PanelState } from "../client";

/** 全局状态影响写入资格，局部业务结果仍留在对象自身。 */
export function blocked(state: PanelState): string | undefined {
  if (!state.online) return "与本机服务的连接中断，恢复后才能操作。";
  if (!state.snapshot?.authority_available) return "数据无法保存，暂时不能发起新操作。";
  if (state.snapshot.phase !== "running") return "Engine 尚未就绪或正在关闭。";
  return undefined;
}
/** 生命周期停止必须能打断其它控制；这里只合并相同操作，不能锁住整个设备或活动。 */
export function targetReason(state: PanelState, target: string, operation: string): string | undefined {
  return blocked(state) ?? (state.entries.some((entry) => entry.request.instance_id === state.snapshot?.instance_id && entry.request.target === target && entry.request.operation === operation && awaiting(entry)) ? "同样的操作还在进行，请等它完成。" : undefined);
}
