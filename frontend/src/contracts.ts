/**
 * 本机 HTTP 的展示契约与浏览器恢复边界。
 * 只读取 Host 有限投影；unknown 必须经过形状/有限数值校验后才能进入页面。
 * 状态字符串保留原值，展示层对未认识的状态明确告警，不补成空闲或成功。
 */
export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
export type ObjectValue = { [key: string]: Json };

/** 当前连续批次的逻辑请求用量与剩余秒数；重试不另占逻辑名额。 */
export interface Budget { max_requests: number; used_requests: number; remaining_seconds: number }
/** 前台或反思的独立观察快照，busy 不代表外部行动已归还。 */
export interface Session { scope: string; generation: number; busy: boolean; closing: boolean; budgets: Budget[] }
/** 主体拥有的运行期开关；时间与状态来自宿主，页面不自行产生空闲刺激。 */
export interface ProactiveThinking {
  enabled: boolean; revision: string; idle_seconds: number; waiting_for_idle: boolean;
  last_idle_at: number | null; last_error: string | null;
}
/** 带截断标记和原文字数的展示预览，不授予完整原文已读资格。 */
export interface PreviewText { text: string; truncated: boolean; original_chars: number }
/** 原能力调用的真实结果版本；unknown 保留待核对效果，不能自动重做。 */
export interface Call {
  call_id: string; path: string; state: string; receipt_revision: number;
  started_at: number | null; finished_at: number | null; error_type: string | null;
  error: PreviewText | null; result: PreviewText | null; media_count: number;
}
/** 一项行动及其逐次真实调用，整体结束不覆盖局部效果。 */
export interface Action {
  action_id: string; state: string; created_at: number; finished_at: number | null;
  error: PreviewText | null; calls: Call[];
}
/** 人工审查展开完整原文，区别于快照的有界预览。 */
export interface Review {
  revision: string; turn_id: string | null;
  stimuli: { input_id: string; adapter_id: string; display_name: string | null; text: string; occurred_at: string; received_at: string; media: { media_id: string; media_type: string; name: string | null }[] }[];
  action: { action_id: string; state: string; source: string; output: string; error: string | null; result_json: string | null;
    calls: { call_id: string; path: string; state: string; arguments_json: string; result_json: string | null; error: string | null }[] };
  feedback: { evidence_id: string; request_id: string; revision: string; rating: "good" | "bad"; comment: string; created_at: number }[];
}

/** 校验完整审查材料，不把缺失原文或未知评分补成成功。 */
export function review(value: unknown): Review {
  const v = object(value, "review");
  const a = object(v.action, "review.action");
  return {
    revision: text(v.revision, "revision"), turn_id: nullable(v.turn_id, "turn_id", text),
    action: {
      action_id: text(a.action_id, "action_id"), state: text(a.state, "state"), source: text(a.source, "source"), output: text(a.output, "output"),
      error: nullable(a.error, "error", text), result_json: nullable(a.result_json, "result_json", text),
      calls: list(a.calls, "calls", (entry, at) => { const c = object(entry, at); return {
        call_id: text(c.call_id, at), path: text(c.path, at), state: text(c.state, at), arguments_json: text(c.arguments_json, at),
        result_json: nullable(c.result_json, at, text), error: nullable(c.error, at, text),
      }; }),
    },
    stimuli: list(v.stimuli, "stimuli", (entry, at) => { const s = object(entry, at); return {
      input_id: text(s.input_id, at), adapter_id: text(s.adapter_id, at), display_name: nullable(s.display_name, at, text),
      text: text(s.text, at), occurred_at: text(s.occurred_at, at), received_at: text(s.received_at, at),
      media: list(s.media, at, (entry, field) => { const m = object(entry, field); return { media_id: text(m.media_id, field), media_type: text(m.media_type, field), name: nullable(m.name, field, text) }; }),
    }; }),
    feedback: list(v.feedback, "feedback", (entry, at) => {
      const f = object(entry, at);
      if (f.rating !== "good" && f.rating !== "bad") throw new ContractError(`${at}.rating`);
      return { evidence_id: text(f.evidence_id, at), request_id: text(f.request_id, at), revision: text(f.revision, at), rating: f.rating,
        comment: text(f.comment, at), created_at: number(f.created_at, at) };
    }),
  };
}

/** 历史审查用服务端游标分页，不以本页记录条数推测还有多少历史。 */
export function reviewPage(value: unknown): { items: Action[]; next_cursor: string | null } {
  const v = object(value, "review_page");
  return { items: list(v.items, "items", action), next_cursor: nullable(v.next_cursor, "next_cursor", text) };
}
/** 权威活动版本、预算与恢复要求，页面不自行推进运行累计。 */
export interface Activity {
  activity_id: string; definition: string; goal: string; state: string; version: number;
  run_generation: number; remaining_seconds: number; budget_basis: string;
  needs_reconciliation: boolean; timing_incomplete: boolean; last_command_id: string;
  progress_preview: PreviewText | null;
  last_command: { command_id: string; command: string; status: string; actual_state: string | null; applied: boolean | null; error: PreviewText | null } | null;
}
/** 宿主显式开放的快捷控制及允许参数字段，区别于完整模型能力目录。 */
export interface DeviceControl { key: string; title: string; fields: string[] }
/** 已装配设备的有限状态与允许控制，读取快照不取得设备连接。 */
export interface Device {
  instance_id: string; kind: string | null; revision: string | null; state: ObjectValue;
  controls: DeviceControl[]; has_preview: boolean;
}
/** 一次成功校验的宿主状态；断线后可只读旧值，控制资格另行复核。 */
export interface Snapshot {
  proactive_thinking: ProactiveThinking | null;
  instance_id: string; host_version: number; phase: string; pending_resources: string[];
  authority_available: boolean; devices: Device[]; foreground: Session | null; reflection: Session | null;
  knowledge: ObjectValue | null; pending_limits: { count: number; logical_bytes: number };
  pending_inputs: { stage: string; count: number; logical_bytes: number }[];
  sessions: { scope: string; version: number; active_turn: string | null }[];
  recent_actions: Action[]; activities: Activity[];
}
/** 首次发送前固定的原控制身份、实例、目标版本与参数，重传保持完整内容。 */
export interface ControlRequest {
  request_id: string; instance_id: string; target: string; operation: string;
  expected_revision: string; arguments: ObjectValue;
}
/** 已关联活动命令的实际确认，accepted 与实际 applied 分别表达。 */
export interface ActivityCommand {
  command_id: string; activity_id: string; status: string; actual_state: string | null;
  applied: boolean | null; error: string | null;
}
/** 控制接管状态及真实业务关联，finished 仅说明控制入口已返回。 */
export interface ControlResult {
  request_id: string; instance_id: string; request: ObjectValue; state: string;
  admitted_at: number; action_id: string | null; command_id: string | null;
  finished_at: number | null; error_code: string | null; error: string | null;
  action?: Action | null; activity_command?: ActivityCommand | null;
}

/** 错误指出失效字段，不回显未知网络正文或凭据。 */
export class ContractError extends Error {
  constructor(field: string) { super(`数据格式无效：${field}。旧状态不能用于控制。`); }
}

function object(value: unknown, field: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) throw new ContractError(field);
  return Object.fromEntries(Object.entries(value));
}
function text(value: unknown, field: string): string {
  if (typeof value !== "string") throw new ContractError(field);
  return value;
}
function number(value: unknown, field: string): number {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) throw new ContractError(field);
  return value;
}
function integer(value: unknown, field: string): number {
  const result = number(value, field);
  if (!Number.isSafeInteger(result)) throw new ContractError(field);
  return result;
}
function bool(value: unknown, field: string): boolean {
  if (typeof value !== "boolean") throw new ContractError(field);
  return value;
}
function nullable<T>(value: unknown, field: string, read: (value: unknown, field: string) => T): T | null {
  return value === null ? null : read(value, field);
}
function list<T>(value: unknown, field: string, read: (value: unknown, field: string) => T): T[] {
  if (!Array.isArray(value) || value.length > 256) throw new ContractError(field);
  return value.map((item: unknown, index: number) => read(item, `${field}[${index}]`));
}

/**
 * 校验插件状态的完整 JSON 对象。
 *
 * @param value HTTP 或 sessionStorage 实际取得的未知值。
 * @param field 错误中使用的字段位置；默认采用此入口的业务名称。
 * @returns 深度和节点数受限的完整 JSON 对象。
 * @throws ContractError 当形状、必需字段、有限数值或容量限制不满足契约。
 */
export function jsonObject(value: unknown, field = "state"): ObjectValue {
  let nodes = 0;
  function read(item: unknown, depth: number): Json {
    if (++nodes > 16384 || depth > 16) throw new ContractError(field);
    if (item === null || typeof item === "string" || typeof item === "boolean") return item;
    if (typeof item === "number" && Number.isFinite(item)) return item;
    if (Array.isArray(item)) return item.map((entry: unknown) => read(entry, depth + 1));
    const record = object(item, field);
    return Object.fromEntries(Object.entries(record).map(([key, entry]) => [key, read(entry, depth + 1)]));
  }
  const record = object(value, field);
  return Object.fromEntries(Object.entries(record).map(([key, item]) => [key, read(item, 1)]));
}
function preview(value: unknown, field: string): PreviewText {
  const v = object(value, field);
  return { text: text(v.text, field), truncated: bool(v.truncated, field), original_chars: integer(v.original_chars, field) };
}
function session(value: unknown, field: string): Session {
  const v = object(value, field);
  return {
    scope: text(v.scope, field), generation: integer(v.generation, field), busy: bool(v.busy, field), closing: bool(v.closing, field),
    budgets: list(v.budgets, field, (entry, at) => {
      const b = object(entry, at);
      return { max_requests: integer(b.max_requests, at), used_requests: integer(b.used_requests, at), remaining_seconds: number(b.remaining_seconds, at) };
    }),
  };
}
function call(value: unknown, field: string): Call {
  const v = object(value, field);
  return {
    call_id: text(v.call_id, field), path: text(v.path, field), state: text(v.state, field), receipt_revision: integer(v.receipt_revision, field),
    started_at: nullable(v.started_at, field, number), finished_at: nullable(v.finished_at, field, number),
    error_type: nullable(v.error_type, field, text), error: nullable(v.error, field, preview), result: nullable(v.result, field, preview),
    media_count: integer(v.media_count, field),
  };
}
/**
 * 校验一份行动与其逐次能力调用记录。
 *
 * @param value HTTP 或 sessionStorage 实际取得的未知值。
 * @param field 错误中使用的字段位置；默认采用此入口的业务名称。
 * @returns 保留原行动状态与各次调用真实回执的记录。
 * @throws ContractError 当形状、必需字段、有限数值或容量限制不满足契约。
 */
export function action(value: unknown, field = "action"): Action {
  const v = object(value, field);
  return {
    action_id: text(v.action_id, field), state: text(v.state, field), created_at: number(v.created_at, field),
    finished_at: nullable(v.finished_at, field, number), error: nullable(v.error, field, preview), calls: list(v.calls, field, call),
  };
}
function activity(value: unknown, field: string): Activity {
  const v = object(value, field);
  return {
    activity_id: text(v.activity_id, field), definition: text(v.definition, field), goal: text(v.goal, field), state: text(v.state, field),
    version: integer(v.version, field), run_generation: integer(v.run_generation, field), remaining_seconds: number(v.remaining_seconds, field),
    budget_basis: text(v.budget_basis, field), needs_reconciliation: bool(v.needs_reconciliation, field),
    timing_incomplete: bool(v.timing_incomplete, field), last_command_id: text(v.last_command_id, field),
    progress_preview: nullable(v.progress_preview, field, preview),
    last_command: nullable(v.last_command, field, (entry, at) => {
      const c = object(entry, at);
      return { command_id: text(c.command_id, at), command: text(c.command, at), status: text(c.status, at), actual_state: nullable(c.actual_state, at, text), applied: nullable(c.applied, at, bool), error: nullable(c.error, at, preview) };
    }),
  };
}
function device(value: unknown, field: string): Device {
  const v = object(value, field);
  return {
    instance_id: text(v.instance_id, field), kind: nullable(v.kind, field, text), revision: nullable(v.revision, field, text),
    state: jsonObject(v.state, field), has_preview: bool(v.has_preview, field),
    controls: list(v.controls, field, (entry, at) => {
      const c = object(entry, at);
      return { key: text(c.key, at), title: text(c.title, at), fields: list(c.fields, at, text) };
    }),
  };
}

/**
 * 校验 GET /api/snapshot 的完整业务快照。
 *
 * @param value HTTP 或 sessionStorage 实际取得的未知值。
 * @returns 完整校验的当前宿主投影，不因字段缺失补造空闲或成功。
 * @throws ContractError 当形状、必需字段、有限数值或容量限制不满足契约。
 */
export function snapshot(value: unknown): Snapshot {
  const v = object(value, "snapshot");
  const limits = object(v.pending_limits, "pending_limits");
  return {
    proactive_thinking: nullable(v.proactive_thinking, "proactive_thinking", (raw, field) => {
      const p = object(raw, field);
      const seconds = number(p.idle_seconds, `${field}.idle_seconds`);
      if (seconds <= 0) throw new ContractError(`${field}.idle_seconds`);
      return { enabled: bool(p.enabled, field), revision: text(p.revision, field),
        idle_seconds: seconds, waiting_for_idle: bool(p.waiting_for_idle, field),
        last_idle_at: nullable(p.last_idle_at, field, number), last_error: nullable(p.last_error, field, text) };
    }),
    instance_id: text(v.instance_id, "instance_id"), host_version: integer(v.host_version, "host_version"), phase: text(v.phase, "phase"),
    pending_resources: list(v.pending_resources, "pending_resources", text), authority_available: bool(v.authority_available, "authority_available"),
    devices: list(v.devices, "devices", device), foreground: nullable(v.foreground, "foreground", session), reflection: nullable(v.reflection, "reflection", session),
    knowledge: nullable(v.knowledge, "knowledge", jsonObject), pending_limits: { count: integer(limits.count, "pending_limits.count"), logical_bytes: integer(limits.logical_bytes, "pending_limits.logical_bytes") },
    pending_inputs: list(v.pending_inputs, "pending_inputs", (entry, at) => {
      const p = object(entry, at);
      return { stage: text(p.stage, at), count: integer(p.count, at), logical_bytes: integer(p.logical_bytes, at) };
    }),
    sessions: list(v.sessions, "sessions", (entry, at) => {
      const s = object(entry, at);
      return { scope: text(s.scope, at), version: integer(s.version, at), active_turn: nullable(s.active_turn, at, text) };
    }),
    recent_actions: list(v.recent_actions, "recent_actions", action), activities: list(v.activities, "activities", activity),
  };
}
/**
 * 校验本机控制会话的实例身份与令牌。
 *
 * @param value HTTP 或 sessionStorage 实际取得的未知值。
 * @returns 当前宿主实例与本机控制令牌；令牌只留在内存。
 * @throws ContractError 当形状、必需字段、有限数值或容量限制不满足契约。
 */
export function credentials(value: unknown): { token: string; instance_id: string } {
  const v = object(value, "session");
  return { token: text(v.token, "token"), instance_id: text(v.instance_id, "instance_id") };
}
/**
 * 校验控制受理记录及其关联真实结果。
 *
 * @param value HTTP 或 sessionStorage 实际取得的未知值。
 * @returns 原受理身份及当前关联业务回执，HTTP 完成不代表效果完成。
 * @throws ContractError 当形状、必需字段、有限数值或容量限制不满足契约。
 */
export function controlResult(value: unknown): ControlResult {
  const v = object(value, "control");
  const result: ControlResult = {
    request_id: text(v.request_id, "request_id"), instance_id: text(v.instance_id, "instance_id"), request: jsonObject(v.request, "request"),
    state: text(v.state, "control.state"), admitted_at: number(v.admitted_at, "admitted_at"), action_id: nullable(v.action_id, "action_id", text),
    command_id: nullable(v.command_id, "command_id", text), finished_at: nullable(v.finished_at, "finished_at", number),
    error_code: nullable(v.error_code, "error_code", text), error: nullable(v.error, "error", text),
  };
  if ("action" in v) result.action = nullable(v.action, "action", action);
  if ("activity_command" in v) result.activity_command = nullable(v.activity_command, "activity_command", (entry, at) => {
    const c = object(entry, at);
    return { command_id: text(c.command_id, at), activity_id: text(c.activity_id, at), status: text(c.status, at), actual_state: nullable(c.actual_state, at, text), applied: nullable(c.applied, at, bool), error: nullable(c.error, at, text) };
  });
  return result;
}
/**
 * 校验刷新时恢复的完整原控制请求。
 *
 * @param value HTTP 或 sessionStorage 实际取得的未知值。
 * @returns 可按原身份原参数重传的完整控制请求。
 * @throws ContractError 当形状、必需字段、有限数值或容量限制不满足契约。
 */
export function controlRequest(value: unknown): ControlRequest {
  const v = object(value, "saved_request");
  const request = {
    request_id: text(v.request_id, "request_id"), instance_id: text(v.instance_id, "instance_id"), target: text(v.target, "target"),
    operation: text(v.operation, "operation"), expected_revision: text(v.expected_revision, "expected_revision"), arguments: jsonObject(v.arguments, "arguments"),
  };
  if (!/^[A-Za-z0-9_-]{1,128}$/.test(request.request_id) || !request.target || !request.instance_id || !request.operation || !request.expected_revision) throw new ContractError("saved_request");
  return request;
}
