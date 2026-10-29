/**
 * 页面观察寿命与稳定控制身份的所有者。
 * GET 轮询不打开设备；连接状态只来自成功校验的快照。POST 被浏览器中断后仍由
 * Host 拥有工作，页面保留原请求并提供查询/原样重传。令牌与业务事实分开保存。
 */
import { controlRequest, controlResult, credentials, snapshot, review, reviewPage, ContractError } from "./contracts";
import type { Review, Action } from "./contracts";
import type { ControlRequest, ControlResult, ObjectValue, Snapshot } from "./contracts";

/** 本标签页持有的原控制与观察进度，业务所有权仍归宿主。 */
export interface Entry {
  request: ControlRequest; title: string; result: ControlResult | null;
  sending: boolean; unresolved: boolean; rejected: boolean; unavailable: boolean; error: string | null;
}
/** 页面单一观察者发布的状态，发布后以新顶层快照替换，不在组件内改写。 */
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

/**
 * 判断当前页面控制是否仍须继续观察。
 *
 * 控制 finished 不会替尚在运行的行动或未确认命令结束观察。
 *
 * @param entry 包含原请求、接收状态和关联业务回执的本页记录。
 * @returns 只有受理或真实业务仍未结束时为 true；旧实例且明确查无回执时为 false。
 */
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

  /** 历史审查只读取材料，失败交给对应区域显示，不能伪装空列表。 */
  reviews = async (before: string | null = null): Promise<{ items: Action[]; next_cursor: string | null }> =>
    reviewPage(await responseJson(await this.get(`/api/reviews${before === null ? "" : `?before=${encodeURIComponent(before)}`}`)));

  /** 完整材料必须仍属于用户选择的行动，防止并发查询串用评价目标。 */
  review = async (actionId: string): Promise<Review> => {
    const result = review(await responseJson(await this.get(`/api/reviews/${encodeURIComponent(actionId)}`)));
    if (result.action.action_id !== actionId) throw new ContractError("review.action_id");
    return result;
  };
  /** 返回解除订阅，不借用设备或服务资源。 */
  subscribe = (listener: () => void): (() => void) => { this.listeners.add(listener); return () => this.listeners.delete(listener); };

  private publish(patch: Partial<PanelState>): void {
    this.state = { ...this.state, ...patch };
    this.listeners.forEach((listener) => listener());
  }
  /** 浏览器恢复只装回原身份；格式或存储故障保留可见原因，不自动重发旧业务。 */
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

  /**
   * 刷新并校验宿主快照，合并当前并发观察。
   *
   * 实例与控制会话一致后才标记在线；随后查询原控制身份，不重投业务。
   *
   * @returns 同一轮刷新完成的 Promise；失败在 PanelState 中保留原因和旧快照。
   */
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

  /**
   * 按原控制身份查询真实关联结果。
   *
   * 旧实例且查无回执时保留不可用事实；同实例查无身份仍需用户明确原样重传。
   *
   * @param requestId 当前标签页已保存的原请求身份；未知身份不发起请求。
   * @returns 查询完成的 Promise，故障写回对应条目，不将失败查询当作业务未受理。
   */
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
  /** 先核对原实例、身份和完整参数，再采用回执，避免并发查询将另一操作标成已确认。 */
  private accept(entry: Entry, result: ControlResult): void {
    if (result.request_id !== entry.request.request_id || result.instance_id !== entry.request.instance_id) throw new ContractError("control_identity");
    if (result.request.target !== entry.request.target || result.request.operation !== entry.request.operation || result.request.expected_revision !== entry.request.expected_revision
      || JSON.stringify(result.request.arguments) !== JSON.stringify(entry.request.arguments)) throw new ContractError("control_payload");
    if (result.action && result.action.action_id !== result.action_id) throw new ContractError("action_identity");
    if (result.activity_command && result.activity_command.command_id !== result.command_id) throw new ContractError("command_identity");
    this.update(entry.request.request_id, { result, unresolved: false, unavailable: false, error: null });
  }

  /**
   * 为一个新控制意图分配稳定身份并提交。
   *
   * 首次等待前保存身份并合并同目标同操作；未在线、宿主不就绪或观察名额全满时不新增请求。
   *
   * @param target 宿主声明的活动或设备身份。
   * @param operation 允许的明确操作名称。
   * @param revision 页面实际观察到的目标版本，宿主会在接管时核验。
   * @param argumentsValue 本次明确控制的业务参数，在首次等待前复制。
   * @param title 当前标签页用于展示这次意图的名称。
   * @returns 发送与后续结果观察完成的 Promise；不代表关联业务已经完成。
   */
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

  /**
   * 对仍未确认的原控制进行原样重传。
   *
   * 必须仍属于当前同一宿主实例；已经取得业务结论或仍在发送时不重传。
   *
   * @param requestId 本标签页已保存、仍 unresolved 的原请求身份。
   * @returns 本次发送与观察完成的 Promise，保留原参数和原业务身份。
   */
  retry = async (requestId: string): Promise<void> => {
    const entry = this.state.entries.find((item) => item.request.request_id === requestId);
    if (!entry || entry.sending || !entry.unresolved || !this.state.online || this.session?.instance_id !== entry.request.instance_id) return;
    this.update(requestId, { sending: true, error: null });
    await this.transmit(entry);
  };
  /** 发送仍使用原身份；网络失败保留受理未知，明确 HTTP 拒绝与未知传输分别记账。 */
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
