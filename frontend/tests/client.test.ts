/** 页面传输替身核验双击、网络不确定与刷新恢复；没有真实 HTTP/设备副作用。 */
import { describe, expect, it } from "vitest";
import { PanelClient } from "../src/client";
import { controlRequest } from "../src/contracts";
import { host, result } from "./fixtures";

function memory(initial: string | null = null): { getItem: () => string | null; setItem: (key: string, value: string) => void } {
  let saved = initial;
  return { getItem: () => saved, setItem: (_key, value) => { saved = value; } };
}
function json(value: unknown, status = 200): Response { return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } }); }

describe("只读观察与稳定身份", () => {
  it("数据库重建后旧实例的 16 条缺失回执退出等待，新操作可以提交且不会重放旧请求", async () => {
    const saved = Array.from({ length: 16 }, (_, i) => ({
      request: { request_id: `old-${i}`, instance_id: "old-host", target: "voice", operation: "stop", expected_revision: "1", arguments: {} }, title: "停止收音",
    }));
    const posts: string[] = [];
    let latest: ReturnType<typeof controlRequest> | null = null;
    const client = new PanelClient(async (path, init) => {
      if (path === "/api/session") return json({ instance_id: "host-1", token: "token" });
      if (path === "/api/snapshot") return json(host());
      if (init?.method === "POST") {
        latest = controlRequest(JSON.parse(String(init.body)));
        posts.push(latest.request_id);
        return json(result(latest, "completed"), 202);
      }
      if (latest && path.endsWith(latest.request_id)) return json(result(latest, "completed"));
      return json({ error: "control_missing" }, 404);
    }, memory(JSON.stringify(saved)), () => "new-id");
    await client.refresh();
    expect(posts).toEqual([]);
    expect(client.getState().entries).toHaveLength(16);
    expect(client.getState().entries.every((entry) => entry.unavailable && !entry.unresolved && entry.result === null)).toBe(true);
    await client.retry("old-0");
    await client.submit("voice", "stop", "1", {}, "停止收音");
    expect(posts).toEqual(["new-id"]);
    expect(client.getState().entries[0]?.result?.action?.state).toBe("completed");
    expect(client.getState().controlError).toBeNull();
    expect(client.getState().storageError).toBeNull();
  });

  it("最旧记录未确认时淘汰其他已结束记录；真正满载时提示控制限额，取得结果后自动解除", async () => {
    const saved = Array.from({ length: 16 }, (_, i) => ({
      request: { request_id: `pending-${i}`, instance_id: "host-1", target: `target-${i}`, operation: "start", expected_revision: "1", arguments: {} }, title: "开始",
    }));
    let completed = false;
    const posts: string[] = [];
    const client = new PanelClient(async (path, init) => {
      if (path === "/api/session") return json({ instance_id: "host-1", token: "token" });
      if (path === "/api/snapshot") return json(host());
      if (init?.method === "POST") {
        const r = controlRequest(JSON.parse(String(init.body)));
        posts.push(r.request_id);
        return json(result(r, "completed"), 202);
      }
      if (completed && path.endsWith("pending-3")) return json(result(saved[3]!.request, "completed"));
      return json({ error: "control_missing" }, 404);
    }, memory(JSON.stringify(saved)), () => "new-id");
    await client.refresh();
    await client.submit("voice", "stop", "1", {}, "停止");
    expect(posts).toEqual([]);
    expect(client.getState().controlError).toContain("16");
    expect(client.getState().storageError).toBeNull();
    expect(client.getState().entries.every((entry) => !entry.unavailable)).toBe(true);
    completed = true;
    await client.refresh();
    expect(client.getState().controlError).toBeNull();
    await client.submit("voice", "stop", "1", {}, "停止");
    expect(posts).toEqual(["new-id"]);
    expect(client.getState().entries.some((entry) => entry.request.request_id === "pending-15")).toBe(true);
    expect(client.getState().entries.some((entry) => entry.request.request_id === "pending-3")).toBe(false);
  });

  it("历史回执仍存在时采用实际结果；临时网络故障不能被当成回执消失", async () => {
    const old = { request_id: "old-id", instance_id: "old-host", target: "voice", operation: "stop", expected_revision: "1", arguments: {} };
    let offline = true;
    const client = new PanelClient(async (path) => {
      if (path === "/api/session") return json({ instance_id: "host-1", token: "token" });
      if (path === "/api/snapshot") return json(host());
      if (offline) throw new Error("offline");
      return json(result(old, "completed"));
    }, memory(JSON.stringify([{ request: old, title: "旧操作" }])), () => "new-id");
    await client.refresh();
    expect(client.getState().entries[0]?.unresolved).toBe(true);
    expect(client.getState().entries[0]?.unavailable).toBe(false);
    offline = false;
    await client.refresh();
    expect(client.getState().entries[0]?.result?.action?.state).toBe("completed");
  });

  it("明确拒绝的操作刷新后仍是拒绝，不重新成为无回执等待", async () => {
    const storage = memory();
    let posts = 0;
    const fetcher = async (path: string, init?: RequestInit): Promise<Response> => {
      if (path === "/api/session") return json({ instance_id: "host-1", token: "token" });
      if (path === "/api/snapshot") return json(host());
      if (init?.method === "POST") { posts++; return json({ message: "状态已变化" }, 409); }
      return json({}, 404);
    };
    const client = new PanelClient(fetcher, storage, () => "rejected-id");
    await client.refresh();
    await client.submit("voice", "start", "1", {}, "开始收音");
    const restored = new PanelClient(fetcher, storage, () => "new-id");
    await restored.refresh();
    await restored.retry("rejected-id");
    expect(posts).toBe(1);
    expect(restored.getState().entries[0]?.rejected).toBe(true);
    expect(restored.getState().entries[0]?.unresolved).toBe(false);
  });
  it("打开/刷新只请求会话和快照，断线保留过期数据并拒绝控制", async () => {
    const calls: string[] = [];
    let offline = false;
    const client = new PanelClient(async (path) => {
      calls.push(path);
      if (offline) throw new Error("offline");
      return path === "/api/session" ? json({ instance_id: "host-1", token: "token" }) : json(host());
    }, memory(), () => "r");
    await client.refresh();
    expect(calls).toEqual(["/api/session", "/api/snapshot"]);
    offline = true;
    await client.refresh();
    expect(client.getState().online).toBe(false);
    expect(client.getState().snapshot?.instance_id).toBe("host-1");
    await client.submit("computer", "connect", "1", {}, "连接");
    expect(calls).not.toContain("/api/controls");
  });
  it("双击在首次 await 前合并，只有一个 POST", async () => {
    let posts = 0;
    let release: (() => void) | undefined;
    const wait = new Promise<void>((resolve) => { release = resolve; });
    const client = new PanelClient(async (path, init) => {
      if (path === "/api/session") return json({ instance_id: "host-1", token: "token" });
      if (path === "/api/snapshot") return json(host());
      if (init?.method === "POST") {
        posts++;
        await wait;
        const raw: unknown = JSON.parse(String(init.body));
        return json(result(controlRequest(raw)), 202);
      }
      return json({ error: "control_missing" }, 404);
    }, memory(), () => "same-id");
    await client.refresh();
    const first = client.submit("computer", "connect", "1", {}, "连接");
    await client.submit("computer", "connect", "1", {}, "连接");
    expect(posts).toBe(1);
    release?.();
    await first;
    expect(posts).toBe(1);
  });
  it("网络未确认重传完整原 ID/参数，刷新恢复不自动 POST", async () => {
    const bodies: string[] = [];
    let received = false;
    let last = controlRequest({ request_id: "stable-id", instance_id: "host-1", target: "voice", operation: "start", expected_revision: "2", arguments: { mode: "audio", device: 3 } });
    const storage = memory();
    const fetcher = async (path: string, init?: RequestInit): Promise<Response> => {
      if (path === "/api/session") return json({ instance_id: "host-1", token: "token" });
      if (path === "/api/snapshot") return json(host());
      if (init?.method === "POST") {
        bodies.push(String(init.body));
        if (bodies.length === 1) throw new Error("lost-response");
        const raw: unknown = JSON.parse(String(init.body));
        last = controlRequest(raw); received = true;
        return json(result(last, "completed"), 202);
      }
      return received ? json(result(last, "completed")) : json({ error: "control_missing" }, 404);
    };
    const client = new PanelClient(fetcher, storage, () => "stable-id");
    await client.refresh();
    const args = { mode: "audio", device: 3 };
    await client.submit("voice", "start", "2", args, "开始收音");
    args.device = 9;
    const restored = new PanelClient(fetcher, storage, () => "another-id");
    await restored.refresh();
    expect(bodies).toHaveLength(1);
    await restored.retry("stable-id");
    expect(bodies).toHaveLength(2);
    expect(bodies[1]).toBe(bodies[0]);
    expect(last.arguments.device).toBe(3);
    expect(restored.getState().entries[0]?.unresolved).toBe(false);
  });
  it("停止是独立意图，可以打断尚未返回的开始控制", async () => {
    const operations: string[] = [];
    let release: (() => void) | undefined;
    const wait = new Promise<void>((resolve) => { release = resolve; });
    let identity = 0;
    const client = new PanelClient(async (path, init) => {
      if (path === "/api/session") return json({ instance_id: "host-1", token: "token" });
      if (path === "/api/snapshot") return json(host());
      if (init?.method === "POST") {
        const raw: unknown = JSON.parse(String(init.body));
        const request = controlRequest(raw);
        operations.push(request.operation);
        if (request.operation === "start") await wait;
        return json(result(request), 202);
      }
      return json({ error: "control_missing" }, 404);
    }, memory(), () => `request-${++identity}`);
    await client.refresh();
    const starting = client.submit("voice", "start", "1", {}, "开始");
    await client.submit("voice", "stop", "1", {}, "停止");
    expect(operations).toEqual(["start", "stop"]);
    release?.();
    await starting;
  });
  it("错误业务关联不能覆盖原请求身份", async () => {
    const client = new PanelClient(async (path, init) => {
      if (path === "/api/session") return json({ instance_id: "host-1", token: "token" });
      if (path === "/api/snapshot") return json(host());
      if (init?.method === "POST") {
        const raw: unknown = JSON.parse(String(init.body));
        const request = controlRequest(raw);
        return json({ ...result(request), action_id: "other-action" }, 202);
      }
      return json({ error: "control_missing" }, 404);
    }, memory(), () => "r");
    await client.refresh();
    await client.submit("computer", "connect", "1", {}, "连接");
    expect(client.getState().entries[0]?.result).toBeNull();
    expect(client.getState().entries[0]?.unresolved).toBe(true);
  });
  it("宿主实例变化后不把原请求重新派发到新实例", async () => {
    let instance = "host-1";
    let posts = 0;
    const storage = memory();
    const client = new PanelClient(async (path, init) => {
      if (path === "/api/session") return json({ instance_id: instance, token: "token" });
      if (path === "/api/snapshot") return json(host(instance));
      if (init?.method === "POST") { posts++; throw new Error("offline"); }
      return json({ error: "control_missing" }, 404);
    }, storage, () => "old-id");
    await client.refresh();
    await client.submit("computer", "connect", "1", {}, "连接");
    instance = "host-2";
    await client.refresh();
    await client.retry("old-id");
    expect(posts).toBe(1);
    expect(client.getState().snapshot?.instance_id).toBe("host-2");
  });
  it("接入拒绝不是受理未知，不能提供隐式业务重做", async () => {
    let posts = 0;
    const client = new PanelClient(async (path, init) => {
      if (path === "/api/session") return json({ instance_id: "host-1", token: "token" });
      if (path === "/api/snapshot") return json(host());
      if (init?.method === "POST") { posts++; return json({ error: "stale_revision", message: "状态已变化，请刷新。" }, 409); }
      return json({ error: "control_missing" }, 404);
    }, memory(), () => "r");
    await client.refresh();
    await client.submit("computer", "connect", "1", {}, "连接");
    await client.retry("r");
    expect(posts).toBe(1);
    expect(client.getState().entries[0]?.rejected).toBe(true);
    expect(client.getState().entries[0]?.unresolved).toBe(false);
  });
});
