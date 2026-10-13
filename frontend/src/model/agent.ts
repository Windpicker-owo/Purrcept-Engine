/**
 * 全站唯一的主体状态推导：snapshot → AgentState + 一句话 + 视线焦点。
 * 首页吉祥物、顶栏状态与标签页标题都读取这里，不在组件里另行判断。
 * 只使用快照中的明确事实；缺失字段显示为未提供，不补成空闲或成功。
 */
import type { AgentState } from "@purrcept/cel";
import type { Action, Snapshot } from "../contracts";
import { mark } from "../semantics";
import type { AttentionItem } from "./attention";
import { actionSubjects } from "./names";

/** 吉祥物视线的落点：行动路径、待处理列表，或没有特定目标。 */
export type AgentFocus = "path" | "attention" | null;
export interface AgentView { state: AgentState; headline: string; detail: string; focus: AgentFocus }

/** 最近一项仍在运行的行动；宿主按时间倒序给出 recent_actions。 */
export function runningAction(snapshot: Snapshot): Action | undefined {
  return snapshot.recent_actions.find((action) => action.state === "running");
}

/** 优先级自上而下：连接 → 存储 → 宿主阶段 → 行动 → 思考 → 感知 → 反思 → 待你处理 → 空闲。 */
export function agentView(snapshot: Snapshot | null, online: boolean, attention: readonly AttentionItem[]): AgentView {
  if (snapshot === null) {
    return online
      ? { state: "waiting-external", headline: "正在连接本机服务", detail: "页面不会启动她或打开任何设备。", focus: null }
      : { state: "warning", headline: "还没有连上本机服务", detail: "确认 Engine 正在运行，页面会自动重试。", focus: null };
  }
  if (!online) return { state: "warning", headline: "与本机服务的连接中断", detail: "下面显示的是断开前的状态；她已经接手的工作不会因此取消。", focus: null };
  if (!snapshot.authority_available) return { state: "failed", headline: "数据无法保存", detail: "新的控制和行动已暂停。", focus: "attention" };
  if (snapshot.phase !== "running") {
    const phase = mark(snapshot.phase);
    return { state: phase.state, headline: `Engine ${phase.label}`, detail: "运行就绪后才会接收新的控制。", focus: null };
  }
  const action = runningAction(snapshot);
  if (action !== undefined) {
    const subjects = actionSubjects(action.calls.map((call) => call.path), snapshot);
    return {
      state: "acting",
      headline: subjects.length === 0 ? "正在执行操作" : `正在使用${subjects.join("、")}`,
      detail: `${action.calls.length} 项操作，${action.calls.filter((call) => call.state === "confirmed").length} 项已完成。`,
      focus: "path",
    };
  }
  if (snapshot.foreground === null) return { state: "warning", headline: "主体状态未提供", detail: "宿主没有给出前台会话的状态。", focus: null };
  if (snapshot.foreground.busy) return { state: "reasoning", headline: "正在想怎么回应", detail: "她在组织下一步。", focus: null };
  const listening = snapshot.devices.some((device) => device.kind === "voice_input" && device.state.state === "listening");
  const waiting = snapshot.pending_inputs.filter((item) => item.stage !== "processed" && item.stage !== "failed").reduce((sum, item) => sum + item.count, 0);
  if (waiting > 0) return { state: "perceiving", headline: `收到 ${waiting} 条新消息`, detail: "正在判断要不要回应。", focus: null };
  if (listening) return { state: "perceiving", headline: "正在听", detail: "收音已开启。", focus: null };
  if (snapshot.reflection?.busy) return { state: "reasoning", headline: "正在回顾和整理记忆", detail: "这是独立的反思，不影响她回应新消息。", focus: null };
  const forUser = attention.filter((item) => item.needsUser);
  if (forUser.length > 0) return { state: "waiting-user", headline: `有 ${forUser.length} 件事需要你看一下`, detail: forUser[0]?.title ?? "", focus: "attention" };
  return { state: "idle", headline: "安静待命中", detail: "有新消息或到点的提醒时，她会自己开始。", focus: null };
}
