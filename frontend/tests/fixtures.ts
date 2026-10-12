/** 有限宿主投影替身，不加载真实模型、设备、凭据或现有用户数据。 */
import type { Action, Activity, ControlRequest, ControlResult, Snapshot } from "../src/contracts";

export const paused: Activity = {
  activity_id: "activity-1", definition: "example", goal: "核对本地替身", state: "paused", version: 3,
  run_generation: 1, remaining_seconds: 60, budget_basis: "committed_checkpoint", needs_reconciliation: false,
  timing_incomplete: false, last_command_id: "command-1", progress_preview: null, last_command: null,
};
export function host(instance = "host-1"): Snapshot {
  return {
    instance_id: instance, host_version: 1, phase: "running", authority_available: true, pending_resources: [], devices: [],
    foreground: { scope: "foreground", generation: 1, busy: false, closing: false, budgets: [] }, reflection: null,
    knowledge: null, pending_limits: { count: 8, logical_bytes: 2048 }, pending_inputs: [], sessions: [], recent_actions: [], activities: [paused],
  };
}
export function request(): ControlRequest {
  return { request_id: "request-1", instance_id: "host-1", target: "computer", operation: "connect", expected_revision: "1", arguments: {} };
}
export function receipt(state = "running"): Action {
  return { action_id: "action-1", state, created_at: 1, finished_at: null, error: null, calls: [] };
}
export function result(r: ControlRequest, actionState = "running"): ControlResult {
  return {
    request_id: r.request_id, instance_id: r.instance_id,
    request: { target: r.target, operation: r.operation, expected_revision: r.expected_revision, arguments: r.arguments },
    state: "finished", admitted_at: 1, action_id: "action-1", command_id: null, finished_at: 1, error_code: null, error: null,
    action: receipt(actionState),
  };
}
