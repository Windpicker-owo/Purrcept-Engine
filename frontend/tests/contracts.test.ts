/** 展示边界和恢复资格测试，专门守住未知不能被渲染成成功/空闲的语义。 */
import { describe, expect, it } from "vitest";
import { ContractError, action, controlRequest, jsonObject, modelUsage, snapshot } from "../src/contracts";
import { activityReason, controlMark, mark } from "../src/semantics";
import { awaiting } from "../src/client";
import { host, paused, request, result } from "./fixtures";

describe("真实 JSON 入口", () => {
  it("完整有限投影通过，未知状态保持原文并告警", () => {
    const value = snapshot({ ...host(), phase: "future-phase" });
    expect(value.phase).toBe("future-phase");
    expect(mark(value.phase)).toEqual({ state: "warning", label: "未知状态：future-phase" });
  });
  it("拒绝错误计数、缺失会话字段、非有限数值与过深状态", () => {
    expect(() => snapshot({ ...host(), pending_limits: { count: "8", logical_bytes: 1 } })).toThrow(ContractError);
    expect(() => snapshot({ ...host(), foreground: { scope: "foreground", busy: false } })).toThrow(ContractError);
    expect(() => jsonObject({ level: Infinity })).toThrow(ContractError);
    let nested: unknown = {};
    for (let index = 0; index < 18; index++) nested = { child: nested };
    expect(() => jsonObject(nested)).toThrow(ContractError);
  });
  it("不从行动完成推导逐次调用成功", () => {
    const call = { call_id: "call-1", path: "me.example", state: "unknown", receipt_revision: 1, started_at: 1, finished_at: 2, error_type: null, error: null, result: null, media_count: 0 };
    const projected = action({ action_id: "a", state: "completed", created_at: 1, finished_at: 2, error: null, calls: [call] });
    expect(projected.calls[0]?.state).toBe("unknown");
    expect(mark(projected.calls[0]?.state).state).toBe("warning");
  });
  it("恢复入口拒绝可进入路由的坏请求 ID", () => {
    expect(() => controlRequest({ ...request(), request_id: "../controls" })).toThrow(ContractError);
  });
});
describe("受理与实际结束", () => {
  it("主动思考开关使用真实布尔值和正间隔，不从缺失字段猜测关闭", () => {
    const value = host();
    expect(snapshot(value).proactive_thinking?.enabled).toBe(false);
    expect(() => snapshot({ ...value, proactive_thinking: { ...value.proactive_thinking, enabled: "false" } })).toThrow(ContractError);
    expect(() => snapshot({ ...value, proactive_thinking: { ...value.proactive_thinking, idle_seconds: 0 } })).toThrow(ContractError);
    const { proactive_thinking, ...missing } = value;
    expect(() => snapshot(missing)).toThrow(ContractError);
  });
  it("finished 仍需查询运行中的关联行动", () => {
    const r = result(request());
    expect(controlMark(r).label).toContain("实际结果");
    expect(awaiting({ request: request(), title: "连接", result: r, sending: false, unresolved: false, rejected: false, unavailable: false, error: null })).toBe(true);
  });
  it("活动 accepted 不能显示实际结束，confirmed 才结束观察", () => {
    const r = { ...result(request()), action_id: null, action: null, command_id: "c", activity_command: { command_id: "c", activity_id: "a", status: "accepted", actual_state: null, applied: null, error: null } };
    const entry = { request: request(), title: "暂停", result: r, sending: false, unresolved: false, rejected: false, unavailable: false, error: null };
    expect(awaiting(entry)).toBe(true);
    expect(awaiting({ ...entry, result: { ...r, activity_command: { ...r.activity_command, status: "confirmed", actual_state: "paused", applied: true } } })).toBe(false);
  });
  it("恢复严格保留待核对、耗尽和未完成收尾的阻塞理由", () => {
    expect(activityReason(paused, "resume")).toBeUndefined();
    expect(activityReason({ ...paused, needs_reconciliation: true }, "resume")).toContain("核对");
    expect(activityReason({ ...paused, remaining_seconds: 0 }, "resume")).toContain("用完");
    expect(activityReason({ ...paused, state: "pause_requested" }, "resume")).toContain("停稳");
    expect(activityReason({ ...paused, state: "completed" }, "end")).toContain("已经结束");
  });
  it("校验模型遥测扩展字段并兼容最小旧版载荷，拒绝越界比率", () => {
    const legacy = modelUsage({
      total: {
        attempts: 2,
        reported_attempts: 2,
        input_tokens: 100,
        output_tokens: 40,
        total_tokens: 140,
        cached_input_tokens: 50,
        cache_write_input_tokens: 0,
        reasoning_tokens: 10,
        cache_hit_rate: 0.5,
      },
      models: [],
    });
    expect(legacy.overview.requests).toBe(2);
    expect(legacy.series).toEqual([]);

    const enriched = modelUsage({
      total: legacy.total,
      models: [{ model: "openai/gpt-4.1", ...legacy.total }],
      overview: {
        requests: 2,
        succeeded_attempts: 2,
        failed_attempts: 0,
        timed_out_attempts: 0,
        running_attempts: 0,
        retry_attempts: 0,
        success_rate: 1.0,
        avg_latency_ms: 420.5,
        p50_latency_ms: 400.0,
        p95_latency_ms: 510.0,
        min_latency_ms: 330.0,
        max_latency_ms: 511.0,
        avg_tokens_per_second: 47.6,
        since: 0,
        observed_at: 1800000100,
        bucket_seconds: 3600,
        first_attempt_at: 1800000000,
        last_attempt_at: 1800000050,
      },
      series: [
        {
          start_at: 1800000000,
          end_at: 1800003600,
          ...legacy.total,
          succeeded_attempts: 2,
          failed_attempts: 0,
          retry_attempts: 0,
          avg_latency_ms: 420.5,
          p95_latency_ms: 510.0,
        },
      ],
      model_metrics: [
        {
          model: "openai/gpt-4.1",
          requests: 2,
          ...legacy.total,
          succeeded_attempts: 2,
          failed_attempts: 0,
          timed_out_attempts: 0,
          running_attempts: 0,
          retry_attempts: 0,
          success_rate: 1.0,
          avg_latency_ms: 420.5,
          p50_latency_ms: 400.0,
          p95_latency_ms: 510.0,
          max_latency_ms: 511.0,
          avg_tokens_per_second: 47.6,
          last_used_at: 1800000050,
        },
      ],
      scopes: [
        {
          scope: "foreground",
          requests: 2,
          ...legacy.total,
          succeeded_attempts: 2,
          failed_attempts: 0,
          retry_attempts: 0,
          success_rate: 1.0,
          avg_latency_ms: 420.5,
          p95_latency_ms: 510.0,
        },
      ],
      recent_attempts: [
        {
          attempt_id: "att-1",
          request_id: "req-1",
          model: "openai/gpt-4.1",
          scope: "foreground",
          kind: "generation",
          sequence: 1,
          outcome: "succeeded",
          created_at: 1800000050,
          latency_ms: 420.5,
          has_usage: true,
          input_tokens: 50,
          output_tokens: 20,
          total_tokens: 70,
          cached_input_tokens: 25,
          reasoning_tokens: 5,
        },
      ],
    });
    expect(enriched.overview.p95_latency_ms).toBe(510.0);
    expect(enriched.scopes[0]?.scope).toBe("foreground");
    expect(() =>
      modelUsage({ ...enriched, overview: { ...enriched.overview, success_rate: 1.2 } }),
    ).toThrow(ContractError);
  });
});
