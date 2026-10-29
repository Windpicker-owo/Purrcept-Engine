/** 视图模型的纯函数边界：状态推导、待处理、路由与时间线只依赖有限快照替身。 */
import { describe, expect, it } from "vitest";
import type { Entry } from "../src/client";
import type { Action, Device, Snapshot } from "../src/contracts";
import { agentView } from "../src/model/agent";
import { attentionItems } from "../src/model/attention";
import { deviceSummary } from "../src/model/devices";
import { capabilityName } from "../src/model/names";
import { parseRoute, routeHref } from "../src/model/routes";
import { timelineItems } from "../src/model/timeline";
import { host, paused, request } from "./fixtures";

function action(state: string, calls: [string, string][], id = "action-1", created = 10): Action {
  return {
    action_id: id, state, created_at: created, finished_at: null, error: null,
    calls: calls.map(([path, callState], index) => ({
      call_id: `${id}-call-${index}`, path, state: callState, receipt_revision: 1, started_at: created, finished_at: null,
      error_type: null, error: null, result: null, media_count: 0,
    })),
  };
}
function device(kind: string, state: Device["state"]): Device {
  return { instance_id: kind, kind, revision: "1", state, controls: [], has_preview: false };
}
function entry(patch: Partial<Entry> = {}): Entry {
  return { request: request(), title: "连接电脑", result: null, sending: false, unresolved: false, rejected: false, unavailable: false, error: null, ...patch };
}

describe("agentView", () => {
  it("主体丧失派发资格时禁止把空闲会话显示为安静待命", () => {
    const base = host();
    const failed = { ...base, health: { ...base.health, can_process: false } };
    expect(agentView(failed, true, [])).toMatchObject({ state: "failed", headline: "主体已停止处理" });
    const partial = { ...base, health: { ...base.health, components: [{ component: "memory", state: "failed", failed_at: 1, error_id: "fault", error_code: "component_failed", error_type: "LookupError", can_process: false }] } };
    expect(agentView(partial, true, []).headline).toBe("部分后台工作已暂停");
  });
  it("断线时不把旧快照当作当前状态", () => {
    expect(agentView(host(), false, []).state).toBe("warning");
    expect(agentView(null, false, []).headline).toContain("还没有连上");
  });
  it("存储不可用优先于一切运行状态", () => {
    const s: Snapshot = { ...host(), authority_available: false, recent_actions: [action("running", [["me.qq.send", "started"]])] };
    expect(agentView(s, true, []).state).toBe("failed");
  });
  it("在途行动显示涉及的对象并把视线交给行动路径", () => {
    const s: Snapshot = { ...host(), recent_actions: [action("running", [["me.qq.send_message", "confirmed"], ["me.notebook.write", "started"]])] };
    const view = agentView(s, true, []);
    expect(view).toMatchObject({ state: "acting", headline: "正在使用QQ、笔记", focus: "path" });
    expect(view.detail).toContain("1 项已完成");
  });
  it("前台忙碌是思考，积压输入是感知，空闲时不伪造活动", () => {
    const base = host();
    expect(agentView({ ...base, foreground: { ...base.foreground!, busy: true } }, true, []).state).toBe("reasoning");
    expect(agentView({ ...base, pending_inputs: [{ stage: "attention", count: 2, logical_bytes: 10 }] }, true, []).state).toBe("perceiving");
    expect(agentView({ ...base, pending_inputs: [{ stage: "submitted", count: 2, logical_bytes: 10 }] }, true, []).state).toBe("idle");
    expect(agentView(base, true, []).state).toBe("idle");
  });
  it("只有需要用户本人处理的事项才进入等你确认", () => {
    const s: Snapshot = { ...host(), activities: [{ ...paused, needs_reconciliation: true }] };
    expect(agentView(s, true, attentionItems(s, [])).state).toBe("idle");
    const unknown: Snapshot = { ...host(), recent_actions: [action("completed", [["me.qq.send", "unknown"]])] };
    expect(agentView(unknown, true, attentionItems(unknown, [])).state).toBe("waiting-user");
  });
  it("缺少前台会话时如实提示而不是显示空闲", () => {
    expect(agentView({ ...host(), foreground: null }, true, []).headline).toBe("主体状态未提供");
  });
});

describe("attentionItems", () => {
  it("组件故障进入可导航的待处理事项，不只留在空闲状态文案中", () => {
    const s = host();
    s.health.components = [{ component:"memory",state:"failed",failed_at:1,error_id:"fault",error_code:"component_failed",error_type:"LookupError",can_process:false }];
    expect(attentionItems(s, []).find((item) => item.id === "component-health")).toMatchObject({ title:"部分后台工作已暂停",route:{page:"system",id:null},needsUser:true });
  });
  it("汇总存储、设备、未知结果、活动与容量", () => {
    const s: Snapshot = {
      ...host(), authority_available: false,
      devices: [device("qq", { state: "exhausted" }), device("audio", { state: "stopped" })],
      recent_actions: [action("completed", [["me.qq.send", "unknown"], ["me.qq.send", "confirmed"]])],
      activities: [{ ...paused, state: "budget_exhausted" }],
      pending_inputs: [{ stage: "attention", count: 8, logical_bytes: 10 }],
    };
    const ids = attentionItems(s, []).map((item) => item.id);
    expect(ids).toEqual(["storage", "device:qq", "action:action-1", "activity:activity-1:budget", "capacity"]);
    expect(attentionItems(s, []).find((item) => item.id === "action:action-1")?.impact).toContain("不会自动重做");
  });
  it("只收录当前宿主实例的未决控制", () => {
    const current = entry({ unresolved: true, error: "网络中断" });
    const old = entry({ unresolved: true, request: { ...request(), request_id: "old", instance_id: "host-0" } });
    expect(attentionItems(host(), [current, old]).map((item) => item.id)).toEqual(["control:request-1"]);
    expect(attentionItems(host(), [entry({ unresolved: true, sending: true })])).toEqual([]);
  });
  it("没有快照时不推导任何事项", () => {
    expect(attentionItems(null, [entry({ rejected: true })])).toEqual([]);
  });
});

describe("voice device facts", () => {
  it("收音处理失败不能显示成回应，原声不可用时给出文字模式入口", () => {
    const lines = deviceSummary(device("voice_input", {
      mode: "audio", device: 5, devices: [{ index: 5, name: "测试麦克风" }], audio_ready: false,
      latest_input: { processing_stage: "failed" },
    }));
    expect(lines).toContain("收音设备：测试麦克风");
    expect(lines).toContain("最近一句处理失败，不能视为已经回应");
    expect(lines).toContain("直接听原声尚未就绪，请选择“识别成文字”");
  });
  it("语音准备不等于已播放，输入处理完成不等于已发声", () => {
    expect(deviceSummary(device("audio", { active: { stage: "synthesizing", started: false } }))).toContain("正在准备语音，尚未开始播放");
    expect(deviceSummary(device("audio", { active: { started: true } }))).toContain("播放器已开始输出");
    expect(deviceSummary(device("voice_input", { latest_input: { processing_stage: "processed" } }))).toContain("最近一句已处理；是否发声以语音输出状态为准");
  });
});

describe("routes", () => {
  it("解析与生成可往返的深链", () => {
    const route = { page: "devices" as const, id: "qq:main/1" };
    expect(parseRoute(routeHref(route))).toEqual(route);
    expect(parseRoute("")).toEqual({ page: "home", id: null });
    expect(parseRoute("#/unknown/x")).toEqual({ page: "home", id: null });
    expect(parseRoute("#/timeline/%E0%A4%A")).toEqual({ page: "timeline", id: null });
  });
});

describe("names and timeline", () => {
  it("能力路径优先按已装配实例命名，未知命名空间保持原文", () => {
    const s: Snapshot = { ...host(), devices: [{ ...device("qq", {}), instance_id: "friends" }] };
    expect(capabilityName("me.friends.send", s)).toBe("QQ");
    expect(capabilityName("me.browser.open", s)).toBe("浏览器");
    expect(capabilityName("me.custom_tool.run", s)).toBe("custom_tool");
  });
  it("合并行动与控制并按时间倒序，未受理的控制排在最前", () => {
    const s: Snapshot = { ...host(), recent_actions: [action("completed", [], "a2", 20), action("completed", [["me.qq.send", "unknown"]], "a1", 10)] };
    const items = timelineItems(s, [entry()], "all");
    expect(items.map((item) => item.id)).toEqual(["control:request-1", "a2", "a1"]);
    expect(timelineItems(s, [entry()], "check").map((item) => item.id)).toEqual(["a1"]);
    expect(timelineItems(s, [entry()], "controls")).toHaveLength(1);
    expect(items[1]?.title).toBe("一次没有外部操作的行动");
  });
});
