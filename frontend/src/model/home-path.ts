/**
 * 首页的有限行动摘要：保留调用身份与原始顺序，优先留下异常和在途操作。
 * 省略数量始终可见，超过预算的异常单独计数；摘要不是完整执行链，完整回执仍在时间线。
 */
import type { Action, Call } from "../contracts";

/** visible 是原调用的有序子集；hiddenAttention 防止未展示的失败被误读成成功。 */
export interface PathSummary {
  visible: readonly Call[];
  omitted: number;
  hiddenAttention: number;
}

/** 最多展示四次调用；未知和失败优先，然后在途，再按原顺序补足。 */
export function summarizePath(action: Action): PathSummary {
  const needsAttention = (call: Call): boolean => call.state === "unknown" || call.state === "failed";
  const pending = (call: Call): boolean => !["confirmed", "not_started", "unknown", "failed"].includes(call.state);
  const prioritized = [...action.calls.filter(needsAttention), ...action.calls.filter(pending), ...action.calls];
  const selected = new Set<string>();
  for (const call of prioritized) {
    if (selected.size === 4) break;
    selected.add(call.call_id);
  }
  return {
    visible: action.calls.filter((call) => selected.has(call.call_id)),
    omitted: action.calls.length - selected.size,
    hiddenAttention: action.calls.filter((call) => !selected.has(call.call_id) && needsAttention(call)).length,
  };
}
