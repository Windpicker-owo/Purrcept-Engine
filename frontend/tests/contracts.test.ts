/** 展示边界和恢复资格测试，专门守住未知不能被渲染成成功/空闲的语义。 */
import { describe, expect, it } from "vitest";
import { ContractError, action, controlRequest, jsonObject, snapshot } from "../src/contracts";
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
  it("finished 仍需查询运行中的关联行动", () => {
    const r = result(request());
    expect(controlMark(r).label).toContain("实际结果");
    expect(awaiting({ request: request(), title: "连接", result: r, sending: false, unresolved: false, rejected: false, error: null })).toBe(true);
  });
  it("活动 accepted 不能显示实际结束，confirmed 才结束观察", () => {
    const r = { ...result(request()), action_id: null, action: null, command_id: "c", activity_command: { command_id: "c", activity_id: "a", status: "accepted", actual_state: null, applied: null, error: null } };
    const entry = { request: request(), title: "暂停", result: r, sending: false, unresolved: false, rejected: false, error: null };
    expect(awaiting(entry)).toBe(true);
    expect(awaiting({ ...entry, result: { ...r, activity_command: { ...r.activity_command, status: "confirmed", actual_state: "paused", applied: true } } })).toBe(false);
  });
  it("恢复严格保留待核对、耗尽和未完成收尾的阻塞理由", () => {
    expect(activityReason(paused, "resume")).toBeUndefined();
    expect(activityReason({ ...paused, needs_reconciliation: true }, "resume")).toContain("核对");
    expect(activityReason({ ...paused, remaining_seconds: 0 }, "resume")).toContain("耗尽");
    expect(activityReason({ ...paused, state: "pause_requested" }, "resume")).toContain("收尾");
    expect(activityReason({ ...paused, state: "completed" }, "end")).toContain("已经结束");
  });
});
