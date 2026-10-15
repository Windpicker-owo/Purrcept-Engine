/**
 * 页面观察寿命与稳定控制身份的所有者。
 * GET 轮询不打开设备；连接状态只来自成功校验的快照。POST 被浏览器中断后仍由
 * Host 拥有工作，页面保留原请求并提供查询/原样重传。令牌与业务事实分开保存。
 */
import { controlRequest, controlResult, credentials, snapshot, ContractError } from "./contracts";
import type { ControlRequest, ControlResult, ObjectValue, Snapshot } from "./contracts";

export interface Entry {
  request: ControlRequest; title: string; result: ControlResult | null;
  sending: boolean; unresolved: boolean; rejected: boolean; unavailable: boolean; error: string | null;
}
export interface PanelState {
  snapshot: Snapshot | null; online: boolean; receivedAt: number | null; error: string | null;
  entries: Entry[]; storageError: string | null; controlError: string | null;
}
interface StoragePort { getItem(key: string): string | null; setItem(key: string, value: string): void }
type FetchPort = (input: string, init?: RequestInit) => Promise<Response>;
const STORAGE_KEY = "purrcept-panel-controls-v1";

/** HTTP 业务拒绝可以解释，网络/解析故障保留“受理未知”。 */
export class HttpError extends Error {
  constructor(readonly status: number, message: string) { super(message); }
}
async function responseJson(response: Response): Promise<unknown> {
  const value: unknown = await response.json();
  if (!response.ok) {
    const message = typeof value === "object" && value !== null && "message" in value && typeof value.message === "string"
      ? value.message : `请求返回 HTTP ${response.status}。`;
    throw new HttpError(response.status, message);
  }
  return value;
}
function message(error: unknown): string {
  if (error instanceof ContractError || error instanceof HttpError) return error.message;
  return "无法取得本机服务回应；受理与实际结果仍需查询。";
}

/** 是否仍有业务工作等待确认；控制 finished 本身不能结束观察。 */
export function awaiting(entry: Entry): boolean {
  // 旧实例且当前账本明确查无记录，只能保留未知说明，不能无限占用新控制名额。
  if (entry.unavailable) return false;
  if (entry.unresolved || entry.sending) return true;
  const r = entry.result;
  if (r === null) return false;
  if (r.state === "admitted" || r.state === "running") return true;
  if (r.action_id && (!r.action || r.action.state === "running")) return true;
  if (r.command_id && (!r.activity_command || ["admitted", "accepted"].includes(r.activity_command.status))) return true;
  return false;
}

/**
 * React 挂载一次，页面卸载只清理轮询，绝不发送业务取消或释放设备。
 * 状态修改经单一 publish 通知；同目标在途时点击合并，重传保留完整原请求。
 */
export class PanelClient {
  private state: PanelState = { snapshot: null, online: false, receivedAt: null, error: null, entries: [], storageError: null, controlError: null };
  private listeners = new Set<() => void>();
  private session: { token: string; instance_id: string } | null = null;
  private refreshing: Promise<void> | null = null;

  constructor(private readonly fetcher: FetchPort, private readonly storage: StoragePort, private readonly identity: () => string) {
    this.restore();
  }
  /** React 获取不可变顶层展示快照；业务缓存不在组件内重建。 */
  getState = (): PanelState => this.state;
  /** 返回解除订阅，不借用设备或服务资源。 */
  subscribe = (listener: () => void): (() => void) => { this.listeners.add(listener); return () => this.listeners.delete(listener); };

  private publish(patch: Partial<PanelState>): void {
    this.state = { ...this.state, ...patch };
    this.listeners.forEach((listener) => listener());
  }
  private restore(): void {
    try {
      const raw = this.storage.getItem(STORAGE_KEY);
      if (raw === null) return;
      const saved: unknown = JSON.parse(raw);
      if (!Array.isArray(saved) || saved.length > 16) throw new ContractError("saved_controls");
      const entries = saved.map((value: unknown): Entry => {
        if (typeof value !== "object" || value === null || !("request" in value) || !("title" in value) || typeof value.title !== "string") throw new ContractError("saved_control");
        const rejected = "rejected" in value && value.rejected === true;
        return { request: controlRequest(value.request), title: value.title, result: null, sending: false, unresolved: !rejected, rejected, unavailable: false, error: rejected ? "原操作未被接受。" : "页面已刷新，正在查询原控制身份。" };
      });
      this.state = { ...this.state, entries };
    } catch (error: unknown) {
      this.state = { ...this.state, storageError: error instanceof ContractError ? error.message : "本标签页无法恢复控制记录；原宿主工作不会因此取消。" };
    }
  }
  private save(entries: Entry[]): void {
    try {
      this.storage.setItem(STORAGE_KEY, JSON.stringify(entries.map(({ request, title, rejected }) => ({ request, title, rejected }))));
    } catch {
      this.publish({ storageError: "浏览器未保存控制身份。当前页面仍可去重；刷新后请在近期回执核对实际结果。" });
    }
  }
  private update(requestId: string, patch: Partial<Entry>): void {
    const entries = this.state.entries.map((entry) => entry.request.request_id === requestId ? { ...entry, ...patch } : entry);
    this.publish({ entries, controlError: entries.some((entry) => !awaiting(entry)) ? null : this.state.controlError });
    this.save(entries);
  }
  private async get(path: string, init?: RequestInit): Promise<Response> {
    // 超时只结束 HTTP 观察。已接管业务没有页面所有的 AbortSignal。
    return await this.fetcher(path, { cache: "no-store", ...init, signal: AbortSignal.timeout(8000) });
  }

  /** 合并并发刷新；会话/快照实例一致才开放控制，旧快照保持可阅读。 */
  refresh = (): Promise<void> => {
    if (this.refreshing !== null) return this.refreshing;
    this.refreshing = this.readSnapshot().finally(() => { this.refreshing = null; });
    return this.refreshing;
  };
  private async readSnapshot(): Promise<void> {
    try {
      if (this.session === null) this.session = credentials(await responseJson(await this.get("/api/session")));
      const current = snapshot(await responseJson(await this.get("/api/snapshot")));
      if (current.instance_id !== this.session.instance_id) {
        this.session = credentials(await responseJson(await this.get("/api/session")));
        if (current.instance_id !== this.session.instance_id) throw new ContractError("instance_id");
      }
      this.publish({ snapshot: current, online: true, receivedAt: Date.now(), error: null });
      // 查询原身份不会重投。未知回执也周期复查，因为迟到事实可能澄清原结果。
      await Promise.all(this.state.entries.map((entry) => this.observe(entry.request.request_id)));
    } catch (error: unknown) {
      this.session = null;
      this.publish({ online: false, error: message(error) });
    }
  }

  /** 取得当前关联的真实业务结果；不以按钮意图推断设备已改变。 */
  observe = async (requestId: string): Promise<void> => {
    const entry = this.state.entries.find((item) => item.request.request_id === requestId);
    if (!entry || entry.sending || entry.rejected) return;
    try {
      const response = await this.get(`/api/controls/${encodeURIComponent(requestId)}`);
      if (response.status === 404) {
        const unavailable = this.session !== null && entry.request.instance_id !== this.session.instance_id;
        this.update(requestId, {
          unavailable, unresolved: !unavailable,
          error: unavailable
            ? "历史回执不可用：这条操作属于旧 Engine 实例，当前数据库没有对应记录。实际效果无法确认，不会重发，也不占用新操作名额。"
            : "宿主尚无这个控制身份；可使用原 ID 原参数重传。",
        });
        return;
      }
      const result = controlResult(await responseJson(response));
      this.accept(entry, result);
    } catch (error: unknown) {
      this.update(requestId, { error: message(error) });
    }
  };
  private accept(entry: Entry, result: ControlResult): void {
    if (result.request_id !== entry.request.request_id || result.instance_id !== entry.request.instance_id) throw new ContractError("control_identity");
    if (result.request.target !== entry.request.target || result.request.operation !== entry.request.operation || result.request.expected_revision !== entry.request.expected_revision
      || JSON.stringify(result.request.arguments) !== JSON.stringify(entry.request.arguments)) throw new ContractError("control_payload");
    if (result.action && result.action.action_id !== result.action_id) throw new ContractError("action_identity");
    if (result.activity_command && result.activity_command.command_id !== result.command_id) throw new ContractError("command_identity");
    this.update(entry.request.request_id, { result, unresolved: false, unavailable: false, error: null });
  }

  /** 新意图在首次网络 await 前取得并保存身份，双击不会产生第二个请求。 */
  submit = async (target: string, operation: string, revision: string, argumentsValue: ObjectValue, title: string): Promise<void> => {
    if (!this.state.online || this.session === null || this.state.snapshot?.phase !== "running" || !this.state.snapshot.authority_available) return;
    // 去重按具体操作合并；停止和结束是独立意图，必须能打断正在启动/收尾的其它操作。
    if (this.state.entries.some((entry) => entry.request.instance_id === this.session?.instance_id && entry.request.target === target && entry.request.operation === operation && awaiting(entry))) return;
    const entry: Entry = {
      request: { request_id: this.identity(), instance_id: this.session.instance_id, target, operation, expected_revision: revision, arguments: structuredClone(argumentsValue) },
      title, result: null, sending: true, unresolved: true, rejected: false, unavailable: false, error: null,
    };
    const retained = [...this.state.entries];
    if (retained.length >= 16) {
      // 近期列表只淘汰最旧的非在途观察，不因队尾一条未知请求锁死整个面板。
      let removable = retained.length - 1;
      while (removable >= 0 && awaiting(retained[removable]!)) removable--;
      if (removable < 0) {
        this.publish({ controlError: "已有 16 项操作仍在等待结果，请在时间线查询原操作。" });
        return;
      }
      retained.splice(removable, 1);
    }
    const entries = [entry, ...retained];
    this.publish({ entries, controlError: null });
    this.save(entries);
    await this.transmit(entry);
  };

  /** 网络未确认时原样重传；宿主实例变化或业务已返回后不进行重做。 */
  retry = async (requestId: string): Promise<void> => {
    const entry = this.state.entries.find((item) => item.request.request_id === requestId);
    if (!entry || entry.sending || !entry.unresolved || !this.state.online || this.session?.instance_id !== entry.request.instance_id) return;
    this.update(requestId, { sending: true, error: null });
    await this.transmit(entry);
  };
  private async transmit(entry: Entry): Promise<void> {
    const session = this.session;
    if (session === null || session.instance_id !== entry.request.instance_id) return;
    try {
      const result = controlResult(await responseJson(await this.get("/api/controls", {
        method: "POST", headers: { "Content-Type": "application/json", "X-Purrcept-Token": session.token }, body: JSON.stringify(entry.request),
      })));
      this.accept(entry, result);
    } catch (error: unknown) {
      const rejected = error instanceof HttpError && error.status >= 400 && error.status < 500;
      this.update(entry.request.request_id, { unresolved: !rejected, rejected, error: message(error) });
    } finally {
      this.update(entry.request.request_id, { sending: false });
    }
    await this.observe(entry.request.request_id);
    await this.refresh();
  }
}
