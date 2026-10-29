/** 人工评价边界核验完整材料、真实目标和网络未知后的原身份重传。 */
import { expect, it } from "vitest";
import { PanelClient } from "../src/client";
import { review, reviewPage } from "../src/contracts";
import type { ControlRequest } from "../src/contracts";
import { host, receipt } from "./fixtures";

function material() {
  return { revision: "frozen-version", turn_id: "turn", stimuli: [{ input_id: "input", adapter_id: "test", display_name: "审查样本", text: "原文".repeat(600), occurred_at: "2026-10-28T00:00:00Z", received_at: "2026-10-28T00:00:01Z", media: [] }],
    action: { action_id: "action-1", state: "completed", source: "await me.send()", output: "", error: null, result_json: null,
      calls: [{ call_id: "call", path: "me.send", state: "confirmed", arguments_json: '{"text":"完整参数"}', result_json: '{"sent":true}', error: null }] },
    feedback: [{ evidence_id: "human-feedback:old", request_id: "old", revision: "frozen-version", rating: "bad", comment: "请等待回应。", created_at: 1 }] };
}
function json(value: unknown): Response { return new Response(JSON.stringify(value), { headers: { "Content-Type": "application/json" } }); }

it("审查原文超过快照预览长度仍完整保留，未知评分和缺失原文必须拒绝", () => {
  expect(review(material()).stimuli[0]?.text.length).toBe(1200);
  expect(reviewPage({ items: [receipt()], next_cursor: "older" }).next_cursor).toBe("older");
  expect(() => review({ ...material(), stimuli: [{ input_id: "missing-text" }] })).toThrow();
  expect(() => review({ ...material(), feedback: [{ ...material().feedback[0], rating: "neutral" }] })).toThrow();
});

it("评价重传保留评分、留言、所见版本与身份，不重新发出行动", async () => {
  const posts: ControlRequest[] = [];
  let stored: unknown = null;
  let lost = true;
  let saved: string | null = null;
  const client = new PanelClient(async (path, init) => {
    if (path === "/api/session") return json({ instance_id: "host-1", token: "token" });
    if (path === "/api/snapshot") return json(host());
    if (path === "/api/reviews/action-1") return json(material());
    if (path === "/api/controls" && init?.method === "POST") {
      const command = JSON.parse(String(init.body)) as ControlRequest;
      posts.push(command);
      stored = { request_id: command.request_id, instance_id: command.instance_id,
        request: { target: command.target, operation: command.operation, expected_revision: command.expected_revision, arguments: command.arguments },
        state: "finished", admitted_at: 1, finished_at: 1, action_id: null, command_id: null, error_code: null, error: null };
      if (lost) throw new Error("lost response");
      return json(stored);
    }
    if (lost) throw new Error("lost query");
    return json(stored);
  }, { getItem: () => saved, setItem: (_key, value) => { saved = value; } }, () => "review-once");
  await client.refresh();
  const seen = await client.review("action-1");
  await client.submit("action:action-1", "review", seen.revision, { rating: "bad", comment: "先问清楚再行动。" }, "行动评价：坏");
  expect(client.getState().entries[0]?.unresolved).toBe(true);
  expect(saved).toContain("先问清楚再行动");
  lost = false;
  await client.retry("review-once");
  expect(posts).toHaveLength(2);
  expect(posts[0]).toEqual(posts[1]);
  expect(posts[0]?.expected_revision).toBe("frozen-version");
  expect(client.getState().entries[0]?.result?.action_id).toBeNull();
  expect(client.getState().entries[0]?.unresolved).toBe(false);
});

it("完整审查返回另一行动时拒绝采用", async () => {
  const client = new PanelClient(async () => json(material()), { getItem: () => null, setItem: () => {} }, () => "id");
  await expect(client.review("another-action")).rejects.toThrow("review.action_id");
});
