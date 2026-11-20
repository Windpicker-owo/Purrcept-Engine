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
export interface ReviewAction {
  action_id: string; state: string; source: string; output: string; error: string | null; result_json: string | null;
  calls: { call_id: string; path: string; state: string; arguments_json: string; result_json: string | null; error: string | null }[];
}
export interface Review {
  revision: string; turn_id: string | null;
  target: { kind: "action" | "turn" | "call"; id: string; state: string };
  stimuli: { input_id: string; adapter_id: string; display_name: string | null; text: string; occurred_at: string; received_at: string; media: { media_id: string; media_type: string; name: string | null }[] }[];
  action: ReviewAction | null; actions: ReviewAction[];
  feedback: { evidence_id: string; request_id: string; revision: string; rating: "good" | "bad"; comment: string; created_at: number }[];
}
/** 目标身份保留种类；没有行动的轮次不会被包装成虚构行动。 */
export interface ReviewItem { target_kind: "action" | "turn" | "call"; target_id: string; state: string; created_at: number; label: string }
function reviewKind(value: unknown): "action" | "turn" | "call" {
  if (value !== "action" && value !== "turn" && value !== "call") throw new ContractError("review.kind");
  return value;
}
/** 将页面选择的真实目标交给统一控制账本。 */
export function reviewTarget(id: string): string { return id.startsWith("turn:") || id.startsWith("call:") ? id : `action:${id}`; }
function reviewAction(value: unknown, at: string): ReviewAction {
  const a = object(value, at);
  return { action_id: text(a.action_id, at), state: text(a.state, at), source: text(a.source, at), output: text(a.output, at),
    error: nullable(a.error, at, text), result_json: nullable(a.result_json, at, text),
    calls: list(a.calls, at, (entry, field) => { const c = object(entry, field); return {
      call_id: text(c.call_id, field), path: text(c.path, field), state: text(c.state, field), arguments_json: text(c.arguments_json, field),
      result_json: nullable(c.result_json, field, text), error: nullable(c.error, field, text),
    }; }),
  };
}
/** 校验完整审查材料与明确目标，缺少行动不会被补成成功。 */
export function review(value: unknown): Review {
  const v = object(value, "review"); const t = object(v.target, "review.target");
  return {
    revision: text(v.revision, "revision"), turn_id: nullable(v.turn_id, "turn_id", text),
    target: { kind: reviewKind(t.kind), id: text(t.id, "target.id"), state: text(t.state, "target.state") },
    action: nullable(v.action, "action", reviewAction), actions: list(v.actions, "actions", reviewAction),
    stimuli: list(v.stimuli, "stimuli", (entry, at) => { const s = object(entry, at); return {
      input_id: text(s.input_id, at), adapter_id: text(s.adapter_id, at), display_name: nullable(s.display_name, at, text),
      text: text(s.text, at), occurred_at: text(s.occurred_at, at), received_at: text(s.received_at, at),
      media: list(s.media, at, (entry, field) => { const m = object(entry, field); return { media_id: text(m.media_id, field), media_type: text(m.media_type, field), name: nullable(m.name, field, text) }; }),
    }; }),
    feedback: list(v.feedback, "feedback", (entry, at) => { const f = object(entry, at);
      if (f.rating !== "good" && f.rating !== "bad") throw new ContractError(`${at}.rating`);
      return { evidence_id: text(f.evidence_id, at), request_id: text(f.request_id, at), revision: text(f.revision, at), rating: f.rating, comment: text(f.comment, at), created_at: number(f.created_at, at) };
    }),
  };
}
/** 校验三种真实目标的服务端分页。 */
export function reviewTargets(value: unknown): { items: ReviewItem[]; next_cursor: string | null } {
  const v = object(value, "review_targets");
  return { items: list(v.items, "items", (entry, at) => { const i = object(entry, at); return { target_kind: reviewKind(i.target_kind), target_id: text(i.target_id, at), state: text(i.state, at), created_at: number(i.created_at, at), label: text(i.label, at) }; }), next_cursor: nullable(v.next_cursor, "next_cursor", text) };
}
/** 历史审查用服务端游标分页，不以本页记录条数推测还有多少历史。 */
export function reviewPage(value: unknown): { items: Action[]; next_cursor: string | null } {
  const v = object(value, "review_page");
  return { items: list(v.items, "items", action), next_cursor: nullable(v.next_cursor, "next_cursor", text) };
}

export type ModelUsagePeriod = "24h" | "7d" | "30d" | "all";

export interface UsageTotals {
  attempts: number;
  reported_attempts: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cached_input_tokens: number;
  cache_write_input_tokens: number;
  reasoning_tokens: number;
  cache_hit_rate: number | null;
}

export interface ModelUsageOverview {
  requests: number;
  succeeded_attempts: number;
  failed_attempts: number;
  timed_out_attempts: number;
  running_attempts: number;
  retry_attempts: number;
  success_rate: number | null;
  avg_latency_ms: number | null;
  p50_latency_ms: number | null;
  p95_latency_ms: number | null;
  min_latency_ms: number | null;
  max_latency_ms: number | null;
  avg_tokens_per_second: number | null;
  since: number;
  observed_at: number;
  bucket_seconds: number;
  first_attempt_at: number | null;
  last_attempt_at: number | null;
}

export interface ModelUsageBucket extends UsageTotals {
  start_at: number;
  end_at: number;
  succeeded_attempts: number;
  failed_attempts: number;
  retry_attempts: number;
  avg_latency_ms: number | null;
  p95_latency_ms: number | null;
}

export interface ModelPerformanceEntry extends UsageTotals {
  model: string;
  requests: number;
  succeeded_attempts: number;
  failed_attempts: number;
  timed_out_attempts: number;
  running_attempts: number;
  retry_attempts: number;
  success_rate: number | null;
  avg_latency_ms: number | null;
  p50_latency_ms: number | null;
  p95_latency_ms: number | null;
  max_latency_ms: number | null;
  avg_tokens_per_second: number | null;
  last_used_at: number | null;
}

export interface ModelScopeEntry extends UsageTotals {
  scope: string;
  requests: number;
  succeeded_attempts: number;
  failed_attempts: number;
  retry_attempts: number;
  success_rate: number | null;
  avg_latency_ms: number | null;
  p95_latency_ms: number | null;
}

export interface ModelRecentAttempt {
  attempt_id: string;
  request_id: string;
  model: string;
  scope: string;
  kind: string;
  sequence: number;
  outcome: string;
  created_at: number;
  latency_ms: number | null;
  has_usage: boolean;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cached_input_tokens: number;
  reasoning_tokens: number;
}

export interface ModelUsage {
  total: UsageTotals;
  models: (UsageTotals & { model: string })[];
  overview: ModelUsageOverview;
  series: ModelUsageBucket[];
  model_metrics: ModelPerformanceEntry[];
  scopes: ModelScopeEntry[];
  recent_attempts: ModelRecentAttempt[];
}

function unitRate(value: unknown, field: string): number | null {
  const rate = nullable(value, field, number);
  if (rate !== null && rate > 1) throw new ContractError(field);
  return rate;
}

/** 校验模型用量接口返回的全局汇总、时序趋势、延迟分位、认知回路与按模型明细。 */
export function modelUsage(value: unknown): ModelUsage {
  const root = object(value, "model_usage");
  if (!Array.isArray(root.models)) throw new ContractError("model_usage.models");
  const total = usageTotals(object(root.total, "model_usage.total"), "model_usage.total");
  const models = root.models.map((entry: unknown, index: number) => {
    const field = `model_usage.models[${index}]`;
    const row = object(entry, field);
    return { model: text(row.model, `${field}.model`), ...usageTotals(row, field) };
  });

  const overview: ModelUsageOverview = root.overview !== undefined
    ? (() => {
        const f = "model_usage.overview";
        const o = object(root.overview, f);
        const bucketSec = integer(o.bucket_seconds, `${f}.bucket_seconds`);
        if (bucketSec <= 0) throw new ContractError(`${f}.bucket_seconds`);
        return {
          requests: integer(o.requests, `${f}.requests`),
          succeeded_attempts: integer(o.succeeded_attempts, `${f}.succeeded_attempts`),
          failed_attempts: integer(o.failed_attempts, `${f}.failed_attempts`),
          timed_out_attempts: integer(o.timed_out_attempts, `${f}.timed_out_attempts`),
          running_attempts: integer(o.running_attempts, `${f}.running_attempts`),
          retry_attempts: integer(o.retry_attempts, `${f}.retry_attempts`),
          success_rate: unitRate(o.success_rate, `${f}.success_rate`),
          avg_latency_ms: nullable(o.avg_latency_ms, `${f}.avg_latency_ms`, number),
          p50_latency_ms: nullable(o.p50_latency_ms, `${f}.p50_latency_ms`, number),
          p95_latency_ms: nullable(o.p95_latency_ms, `${f}.p95_latency_ms`, number),
          min_latency_ms: nullable(o.min_latency_ms, `${f}.min_latency_ms`, number),
          max_latency_ms: nullable(o.max_latency_ms, `${f}.max_latency_ms`, number),
          avg_tokens_per_second: nullable(o.avg_tokens_per_second, `${f}.avg_tokens_per_second`, number),
          since: number(o.since, `${f}.since`),
          observed_at: number(o.observed_at, `${f}.observed_at`),
          bucket_seconds: bucketSec,
          first_attempt_at: nullable(o.first_attempt_at, `${f}.first_attempt_at`, number),
          last_attempt_at: nullable(o.last_attempt_at, `${f}.last_attempt_at`, number),
        };
      })()
    : {
        requests: total.attempts,
        succeeded_attempts: total.reported_attempts,
        failed_attempts: 0,
        timed_out_attempts: 0,
        running_attempts: 0,
        retry_attempts: 0,
        success_rate: null,
        avg_latency_ms: null,
        p50_latency_ms: null,
        p95_latency_ms: null,
        min_latency_ms: null,
        max_latency_ms: null,
        avg_tokens_per_second: null,
        since: 0,
        observed_at: 0,
        bucket_seconds: 3600,
        first_attempt_at: null,
        last_attempt_at: null,
      };

  const series: ModelUsageBucket[] = root.series !== undefined
    ? list(root.series, "model_usage.series", (entry, at) => {
        const b = object(entry, at);
        return {
          start_at: number(b.start_at, `${at}.start_at`),
          end_at: number(b.end_at, `${at}.end_at`),
          ...usageTotals(b, at),
          succeeded_attempts: integer(b.succeeded_attempts, `${at}.succeeded_attempts`),
          failed_attempts: integer(b.failed_attempts, `${at}.failed_attempts`),
          retry_attempts: integer(b.retry_attempts, `${at}.retry_attempts`),
          avg_latency_ms: nullable(b.avg_latency_ms, `${at}.avg_latency_ms`, number),
          p95_latency_ms: nullable(b.p95_latency_ms, `${at}.p95_latency_ms`, number),
        };
      })
    : [];

  const model_metrics: ModelPerformanceEntry[] = root.model_metrics !== undefined
    ? list(root.model_metrics, "model_usage.model_metrics", (entry, at) => {
        const m = object(entry, at);
        return {
          model: text(m.model, `${at}.model`),
          requests: integer(m.requests, `${at}.requests`),
          ...usageTotals(m, at),
          succeeded_attempts: integer(m.succeeded_attempts, `${at}.succeeded_attempts`),
          failed_attempts: integer(m.failed_attempts, `${at}.failed_attempts`),
          timed_out_attempts: integer(m.timed_out_attempts, `${at}.timed_out_attempts`),
          running_attempts: integer(m.running_attempts, `${at}.running_attempts`),
          retry_attempts: integer(m.retry_attempts, `${at}.retry_attempts`),
          success_rate: unitRate(m.success_rate, `${at}.success_rate`),
          avg_latency_ms: nullable(m.avg_latency_ms, `${at}.avg_latency_ms`, number),
          p50_latency_ms: nullable(m.p50_latency_ms, `${at}.p50_latency_ms`, number),
          p95_latency_ms: nullable(m.p95_latency_ms, `${at}.p95_latency_ms`, number),
          max_latency_ms: nullable(m.max_latency_ms, `${at}.max_latency_ms`, number),
          avg_tokens_per_second: nullable(m.avg_tokens_per_second, `${at}.avg_tokens_per_second`, number),
          last_used_at: nullable(m.last_used_at, `${at}.last_used_at`, number),
        };
      })
    : models.map((m) => ({
        ...m,
        requests: m.attempts,
        succeeded_attempts: m.reported_attempts,
        failed_attempts: 0,
        timed_out_attempts: 0,
        running_attempts: 0,
        retry_attempts: 0,
        success_rate: null,
        avg_latency_ms: null,
        p50_latency_ms: null,
        p95_latency_ms: null,
        max_latency_ms: null,
        avg_tokens_per_second: null,
        last_used_at: null,
      }));

  const scopes: ModelScopeEntry[] = root.scopes !== undefined
    ? list(root.scopes, "model_usage.scopes", (entry, at) => {
        const s = object(entry, at);
        return {
          scope: text(s.scope, `${at}.scope`),
          requests: integer(s.requests, `${at}.requests`),
          ...usageTotals(s, at),
          succeeded_attempts: integer(s.succeeded_attempts, `${at}.succeeded_attempts`),
          failed_attempts: integer(s.failed_attempts, `${at}.failed_attempts`),
          retry_attempts: integer(s.retry_attempts, `${at}.retry_attempts`),
          success_rate: unitRate(s.success_rate, `${at}.success_rate`),
          avg_latency_ms: nullable(s.avg_latency_ms, `${at}.avg_latency_ms`, number),
          p95_latency_ms: nullable(s.p95_latency_ms, `${at}.p95_latency_ms`, number),
        };
      })
    : [];

  const recent_attempts: ModelRecentAttempt[] = root.recent_attempts !== undefined
    ? list(root.recent_attempts, "model_usage.recent_attempts", (entry, at) => {
        const a = object(entry, at);
        return {
          attempt_id: text(a.attempt_id, `${at}.attempt_id`),
          request_id: text(a.request_id, `${at}.request_id`),
          model: text(a.model, `${at}.model`),
          scope: text(a.scope, `${at}.scope`),
          kind: text(a.kind, `${at}.kind`),
          sequence: integer(a.sequence, `${at}.sequence`),
          outcome: text(a.outcome, `${at}.outcome`),
          created_at: number(a.created_at, `${at}.created_at`),
          latency_ms: nullable(a.latency_ms, `${at}.latency_ms`, number),
          has_usage: bool(a.has_usage, `${at}.has_usage`),
          input_tokens: integer(a.input_tokens, `${at}.input_tokens`),
          output_tokens: integer(a.output_tokens, `${at}.output_tokens`),
          total_tokens: integer(a.total_tokens, `${at}.total_tokens`),
          cached_input_tokens: integer(a.cached_input_tokens, `${at}.cached_input_tokens`),
          reasoning_tokens: integer(a.reasoning_tokens, `${at}.reasoning_tokens`),
        };
      })
    : [];

  return { total, models, overview, series, model_metrics, scopes, recent_attempts };
}

function usageTotals(row: Record<string, unknown>, field: string): UsageTotals {
  const rate = nullable(row.cache_hit_rate, `${field}.cache_hit_rate`, number);
  if (rate !== null && rate > 1) throw new ContractError(`${field}.cache_hit_rate`);
  return {
    attempts: integer(row.attempts, `${field}.attempts`),
    reported_attempts: integer(row.reported_attempts, `${field}.reported_attempts`),
    input_tokens: integer(row.input_tokens, `${field}.input_tokens`),
    output_tokens: integer(row.output_tokens, `${field}.output_tokens`),
    total_tokens: integer(row.total_tokens, `${field}.total_tokens`),
    cached_input_tokens: integer(row.cached_input_tokens, `${field}.cached_input_tokens`),
    cache_write_input_tokens: integer(row.cache_write_input_tokens, `${field}.cache_write_input_tokens`),
    reasoning_tokens: integer(row.reasoning_tokens, `${field}.reasoning_tokens`),
    cache_hit_rate: rate,
  };
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
/** 组件故障独立于进程生命周期；can_process 与实际派发资格一致。 */
export interface Health {
  instance_id: string; phase: string; can_process: boolean; startup_stage: string | null;
  components: { component: string; state: string; failed_at: number | null; error_id: string | null; error_code: string | null; error_type: string | null; can_process: boolean }[];
}
/** 校验启动或运行期健康，缺失处理资格不能补成正常。 */
export function health(value: unknown): Health {
  const v = object(value, "health");
  return { instance_id: text(v.instance_id, "health.instance_id"), phase: text(v.phase, "health.phase"),
    can_process: bool(v.can_process, "health.can_process"), startup_stage: nullable(v.startup_stage, "startup_stage", text),
    components: list(v.components, "components", (entry, at) => { const c = object(entry, at); return {
      component: text(c.component, at), state: text(c.state, at), failed_at: nullable(c.failed_at, at, number),
      error_id: nullable(c.error_id, at, text), error_code: nullable(c.error_code, at, text),
      error_type: nullable(c.error_type, at, text), can_process: bool(c.can_process, at),
    }; }),
  };
}
/** 一次成功校验的宿主状态；断线后可只读旧值，控制资格另行复核。 */
export interface Snapshot {
  health: Health;
  behavior_review: Session | null; analysis: ObjectValue | null;
  storage_statistics: ObjectValue | null; supervision_observations: ObjectValue[];
  proactive_thinking: ProactiveThinking | null;
  instance_id: string; host_version: number; phase: string; pending_resources: string[];
  authority_available: boolean; devices: Device[]; foreground: Session | null;
  notebooks: Record<string, ObjectValue>; pending_limits: { count: number; logical_bytes: number };
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
    health: health(v.health),
    behavior_review: nullable(v.behavior_review, "behavior_review", session), analysis: nullable(v.analysis,"analysis",jsonObject),
    storage_statistics: nullable(v.storage_statistics, "storage_statistics", jsonObject),
    supervision_observations: list(v.supervision_observations, "supervision_observations", jsonObject),
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
    devices: list(v.devices, "devices", device), foreground: nullable(v.foreground, "foreground", session),
    notebooks: Object.fromEntries(Object.entries(jsonObject(v.notebooks, "notebooks")).map(([key, value]) => [key, jsonObject(value, `notebooks.${key}`)])), pending_limits: { count: integer(limits.count, "pending_limits.count"), logical_bytes: integer(limits.logical_bytes, "pending_limits.logical_bytes") },
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
