/**
 * 页面观察寿命与稳定控制身份的所有者，并承载本机真后台管理通信。
 * GET 轮询只读取会话与快照；各管理页按需调用聊天、配置、记忆与调音接口。
 */
import {
  ContractError,
  controlRequest,
  controlResult,
  credentials,
  health,
  modelUsage,
  review,
  reviewPage,
  reviewTargets,
  snapshot,
} from "./contracts";
import type { Action, ControlRequest, ControlResult, Health, ModelUsage, ModelUsagePeriod, ObjectValue, Review, Snapshot } from "./contracts";
import {
  parseChatFeed,
  parseConfigView,
  parseLive2DPreferencesView,
  parseNotebookFile,
  parseNotebooksOverview,
  parseStudioStatus,
  parseVoiceProfileEnvelope,
  type ChatFeed,
  type ConfigView,
  type Live2DPreferencesData,
  type Live2DPreferencesView,
  type NotebookFileContent,
  type NotebooksOverview,
  type StudioStatus,
  type VisualSavePayload,
  type VoiceProfileData,
  type VoiceProfileEnvelope,
} from "./model/admin";
import { phonePairing, phoneSetup } from "./model/phone";

/** 本标签页持有的原控制与观察进度，业务所有权仍归宿主。 */
export interface Entry {
  request: ControlRequest;
  title: string;
  result: ControlResult | null;
  sending: boolean;
  unresolved: boolean;
  rejected: boolean;
  unavailable: boolean;
  error: string | null;
}

/** 页面单一观察者发布的状态，发布后以新顶层快照替换，不在组件内改写。 */
export interface PanelState {
  startup?: Health | null;
  observedHealth?: Health | null;
  snapshot: Snapshot | null;
  online: boolean;
  receivedAt: number | null;
  error: string | null;
  entries: Entry[];
  storageError: string | null;
  controlError: string | null;
}

interface StoragePort {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}
type FetchPort = (input: string, init?: RequestInit) => Promise<Response>;
const STORAGE_KEY = "purrcept-panel-controls-v1";

/** 健康可连接而快照未刷新时，只改变等待文案，不授予旧状态控制资格。 */
export function isSnapshotDelayed(state: PanelState): boolean {
  return !state.online && state.observedHealth?.phase === "running" && state.observedHealth.startup_stage === null;
}

/** HTTP 业务拒绝可以解释，网络/解析故障保留“受理未知”。 */
export class HttpError extends Error {
  constructor(readonly status: number, messageText: string) {
    super(messageText);
  }
}

async function responseJson(response: Response): Promise<unknown> {
  const value: unknown = await response.json();
  if (!response.ok) {
    const detail =
      typeof value === "object" && value !== null && "message" in value && typeof value.message === "string"
        ? value.message
        : `请求返回 HTTP ${response.status}。`;
    throw new HttpError(response.status, detail);
  }
  return value;
}

function message(error: unknown): string {
  if (error instanceof ContractError || error instanceof HttpError) return error.message;
  return "无法取得本机服务回应；受理与实际结果仍需查询。";
}

/** 判断当前页面控制是否仍须继续观察。 */
export function awaiting(entry: Entry): boolean {
  if (entry.unavailable) return false;
  if (entry.unresolved || entry.sending) return true;
  const r = entry.result;
  if (r === null) return false;
  if (r.state === "admitted" || r.state === "running") return true;
  if (r.action_id && (!r.action || r.action.state === "running")) return true;
  if (r.activity_command && ["admitted", "accepted"].includes(r.activity_command.status)) return true;
  if (r.command_id && !r.activity_command) return true;
  return false;
}

export class PanelClient {
  private state: PanelState = {
    snapshot: null,
    online: false,
    receivedAt: null,
    error: null,
    entries: [],
    storageError: null,
    controlError: null,
  };
  private listeners = new Set<() => void>();
  private session: { token: string; instance_id: string } | null = null;
  private refreshing: Promise<void> | null = null;

  constructor(
    private readonly fetcher: FetchPort,
    private readonly storage: StoragePort,
    private readonly identity: () => string,
  ) {
    this.restore();
  }

  getState = (): PanelState => this.state;

  private async ensureToken(): Promise<string> {
    if (this.session === null) {
      this.session = credentials(await responseJson(await this.get("/api/session")));
    }
    return this.session.token;
  }

  private async adminJsonPost(path: string, body: unknown, timeoutMs = 15000): Promise<unknown> {
    const token = await this.ensureToken();
    const res = await this.fetcher(path, {
      method: "POST",
      cache: "no-store",
      headers: { "Content-Type": "application/json", "X-Purrcept-Token": token },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(timeoutMs),
    });
    return await responseJson(res);
  }

  /** 对话调试台接口。 */
  chatFeed = async (limit = 40): Promise<ChatFeed> =>
    parseChatFeed(await responseJson(await this.get(`/api/chat?limit=${limit}`)));

  sendChat = async (text: string, senderName = "伙伴"): Promise<{ input_id: string }> => {
    const res = await this.adminJsonPost("/api/chat", { text, sender_name: senderName });
    if (typeof res !== "object" || res === null || !("input_id" in res) || typeof res.input_id !== "string") {
      throw new ContractError("chat.send");
    }
    return { input_id: res.input_id };
  };

  /** 配置与人设中心接口。 */
  loadConfig = async (): Promise<ConfigView> =>
    parseConfigView(await responseJson(await this.get("/api/config")));

  saveVisualConfig = async (payload: VisualSavePayload): Promise<ConfigView> =>
    parseConfigView(await this.adminJsonPost("/api/config", payload));

  saveRawConfig = async (revision: string, rawToml: string): Promise<ConfigView> =>
    parseConfigView(await this.adminJsonPost("/api/config", { mode: "raw", revision, raw_toml: rawToml }));

  /** 记忆手账接口。 */
  listNotebooks = async (): Promise<NotebooksOverview> =>
    parseNotebooksOverview(await responseJson(await this.get("/api/notebooks")));

  readNote = async (instance: string, path: string): Promise<NotebookFileContent> =>
    parseNotebookFile(
      await responseJson(
        await this.get(`/api/notebooks/file?instance=${encodeURIComponent(instance)}&path=${encodeURIComponent(path)}`),
      ),
    );

  saveNote = async (
    instance: string,
    path: string,
    content: string,
    revision: string | null,
  ): Promise<{ saved: boolean; revision: string; updated_at: number; path: string }> => {
    const res = await this.adminJsonPost("/api/notebooks/file", { instance, path, content, revision });
    if (
      typeof res !== "object" ||
      res === null ||
      !("revision" in res) ||
      typeof res.revision !== "string" ||
      !("updated_at" in res) ||
      typeof res.updated_at !== "number" ||
      !("path" in res) ||
      typeof res.path !== "string"
    ) {
      throw new ContractError("notebook.save");
    }
    return { saved: true, revision: res.revision, updated_at: res.updated_at, path: res.path };
  };

  /** 内嵌声线调音室与 Live2D 表演接口。 */
  studioStatus = async (): Promise<StudioStatus> =>
    parseStudioStatus(await responseJson(await this.get("/api/studio/status")));

  loadVoiceProfile = async (): Promise<VoiceProfileEnvelope> =>
    parseVoiceProfileEnvelope(await responseJson(await this.get("/api/studio/profile")));

  saveVoiceProfile = async (revision: string, profile: VoiceProfileData): Promise<VoiceProfileEnvelope> =>
    parseVoiceProfileEnvelope(await this.adminJsonPost("/api/studio/profile", { revision, profile }));

  uploadVoiceSource = async (bytes: ArrayBuffer): Promise<{ clip_id: string; duration: number }> => {
    const token = await this.ensureToken();
    const res = await this.fetcher("/api/studio/source", {
      method: "POST",
      cache: "no-store",
      headers: { "Content-Type": "application/octet-stream", "X-Purrcept-Token": token },
      body: bytes,
      signal: AbortSignal.timeout(45000),
    });
    const val = await responseJson(res);
    if (
      typeof val !== "object" ||
      val === null ||
      !("clip_id" in val) ||
      typeof val.clip_id !== "string" ||
      !("duration" in val) ||
      typeof val.duration !== "number"
    ) {
      throw new ContractError("studio.upload");
    }
    return { clip_id: val.clip_id, duration: val.duration };
  };

  voiceOriginalBlob = async (clipId: string): Promise<Blob> => {
    const res = await this.fetcher(`/api/studio/source/${encodeURIComponent(clipId)}`, {
      cache: "no-store",
      signal: AbortSignal.timeout(20000),
    });
    if (!res.ok) {
      await responseJson(res);
    }
    return await res.blob();
  };

  renderVoicePreviewBlob = async (clipId: string, profile: VoiceProfileData): Promise<Blob> => {
    const token = await this.ensureToken();
    const res = await this.fetcher("/api/studio/render", {
      method: "POST",
      cache: "no-store",
      headers: { "Content-Type": "application/json", "X-Purrcept-Token": token },
      body: JSON.stringify({ clip_id: clipId, profile }),
      signal: AbortSignal.timeout(45000),
    });
    if (!res.ok) {
      await responseJson(res);
    }
    return await res.blob();
  };

  loadLive2DPreferences = async (): Promise<Live2DPreferencesView> =>
    parseLive2DPreferencesView(await responseJson(await this.get("/api/live2d/preferences")));

  saveLive2DPreferences = async (revision: string, preferences: Live2DPreferencesData): Promise<Live2DPreferencesView> =>
    parseLive2DPreferencesView(await this.adminJsonPost("/api/live2d/preferences", { revision, preferences }));

  /** 手机配对与引导设置。 */
  phoneSettings = async () => phoneSetup(await responseJson(await this.get("/api/phone/setup")));
  phonePairing = async (target: string, renew = false) =>
    phonePairing(
      await responseJson(
        await this.get(`/api/phone/${encodeURIComponent(target)}/pairing`, renew ? await this.phoneWrite({}) : undefined),
      ),
    );
  replacePhonePairing = async (target: string, deviceId: string) =>
    phonePairing(
      await responseJson(
        await this.get(`/api/phone/${encodeURIComponent(target)}/pairing/replace`, await this.phoneWrite({ device_id: deviceId })),
      ),
    );
  phoneConfigure = async (value: ObjectValue): Promise<Record<string, unknown>> => {
    const result = await responseJson(await this.get("/api/phone/setup", await this.phoneWrite(value)));
    if (typeof result !== "object" || result === null || Array.isArray(result)) throw new ContractError("phone.configure");
    return result as Record<string, unknown>;
  };
  private async phoneWrite(value: ObjectValue): Promise<RequestInit> {
    if (!this.state.online || this.session === null) throw new Error("连接中断，请等待 Engine 重新连接。");
    return {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Purrcept-Token": this.session.token },
      body: JSON.stringify(value),
    };
  }

  reviews = async (before: string | null = null): Promise<{ items: Action[]; next_cursor: string | null }> =>
    reviewPage(await responseJson(await this.get(`/api/reviews${before === null ? "" : `?before=${encodeURIComponent(before)}`}`)));

  /** 按需读取本机账本用量，独立于常规宿主快照。 */
  modelUsage = async (period: ModelUsagePeriod): Promise<ModelUsage> =>
    modelUsage(await responseJson(await this.get(`/api/model-usage?period=${period}`)));

  reviewTargets = async (kind: "action" | "turn" | "call", before: string | null = null) =>
    reviewTargets(await responseJson(await this.get(`/api/reviews?kind=${kind}${before === null ? "" : `&before=${encodeURIComponent(before)}`}`)));

  review = async (actionId: string): Promise<Review> => {
    const result = review(await responseJson(await this.get(`/api/reviews/${encodeURIComponent(actionId)}`)));
    const expected = result.target.kind === "action" ? actionId : actionId.replace(`${result.target.kind}:`, "");
    if (result.target.id !== expected) throw new ContractError("review.target_id");
    return result;
  };

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

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
        if (
          typeof value !== "object" ||
          value === null ||
          !("request" in value) ||
          !("title" in value) ||
          typeof value.title !== "string"
        ) {
          throw new ContractError("saved_control");
        }
        const rejected = "rejected" in value && value.rejected === true;
        return {
          request: controlRequest(value.request),
          title: value.title,
          result: null,
          sending: false,
          unresolved: !rejected,
          rejected,
          unavailable: false,
          error: rejected ? "原操作未被接受。" : "页面已刷新，正在查询原控制身份。",
        };
      });
      this.state = { ...this.state, entries };
    } catch (error: unknown) {
      this.state = {
        ...this.state,
        storageError: error instanceof ContractError ? error.message : "本标签页无法恢复控制记录；原宿主工作不会因此取消。",
      };
    }
  }

  private save(entries: Entry[]): void {
    try {
      this.storage.setItem(
        STORAGE_KEY,
        JSON.stringify(entries.map(({ request, title, rejected }) => ({ request, title, rejected }))),
      );
    } catch {
      this.publish({ storageError: "浏览器未保存控制身份。当前页面仍可去重；刷新后请在近期回执核对实际结果。" });
    }
  }

  private update(requestId: string, patch: Partial<Entry>): void {
    const entries = this.state.entries.map((entry) => (entry.request.request_id === requestId ? { ...entry, ...patch } : entry));
    this.publish({ entries, controlError: entries.some((entry) => !awaiting(entry)) ? null : this.state.controlError });
    this.save(entries);
  }

  private async get(path: string, init?: RequestInit): Promise<Response> {
    return await this.fetcher(path, { cache: "no-store", ...init, signal: AbortSignal.timeout(8000) });
  }

  refresh = (): Promise<void> => {
    if (this.refreshing !== null) return this.refreshing;
    this.refreshing = this.readSnapshot().finally(() => {
      this.refreshing = null;
    });
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
      this.publish({ snapshot: current, online: true, receivedAt: Date.now(), error: null, startup: null, observedHealth: null });
      await Promise.all(this.state.entries.map((entry) => this.observe(entry.request.request_id)));
    } catch (error: unknown) {
      this.session = null;
      try {
        const observed = health(await responseJson(await this.get("/api/health")));
        const starting =
          observed.phase === "starting" ||
          observed.phase === "new" ||
          (observed.phase === "running" && observed.startup_stage !== null);
        const failure =
          observed.phase === "running" && !starting && !(error instanceof ContractError || error instanceof HttpError)
            ? "状态读取未完成，正在重试。"
            : message(error);
        this.publish({
          online: false,
          observedHealth: observed,
          startup: starting || observed.phase === "failed" ? observed : null,
          error: starting && this.state.snapshot === null ? null : failure,
        });
      } catch {
        this.publish({ online: false, error: message(error), startup: null, observedHealth: null });
      }
    }
  }

  observe = async (requestId: string): Promise<void> => {
    const entry = this.state.entries.find((item) => item.request.request_id === requestId);
    if (!entry || entry.sending || entry.rejected) return;
    try {
      const response = await this.get(`/api/controls/${encodeURIComponent(requestId)}`);
      if (response.status === 404) {
        const unavailable = this.session !== null && entry.request.instance_id !== this.session.instance_id;
        this.update(requestId, {
          unavailable,
          unresolved: !unavailable,
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
    if (result.request_id !== entry.request.request_id || result.instance_id !== entry.request.instance_id) {
      throw new ContractError("control_identity");
    }
    if (
      result.request.target !== entry.request.target ||
      result.request.operation !== entry.request.operation ||
      result.request.expected_revision !== entry.request.expected_revision ||
      JSON.stringify(result.request.arguments) !== JSON.stringify(entry.request.arguments)
    ) {
      throw new ContractError("control_payload");
    }
    if (result.action && result.action.action_id !== result.action_id) throw new ContractError("action_identity");
    if (result.activity_command && result.activity_command.command_id !== result.command_id) {
      throw new ContractError("command_identity");
    }
    this.update(entry.request.request_id, { result, unresolved: false, unavailable: false, error: null });
  }

  submit = async (
    target: string,
    operation: string,
    revision: string,
    argumentsValue: ObjectValue,
    title: string,
  ): Promise<void> => {
    if (!this.state.online || this.session === null || this.state.snapshot?.phase !== "running" || !this.state.snapshot.authority_available) {
      return;
    }
    if (
      this.state.entries.some(
        (entry) =>
          entry.request.instance_id === this.session?.instance_id &&
          entry.request.target === target &&
          entry.request.operation === operation &&
          awaiting(entry),
      )
    ) {
      return;
    }
    const entry: Entry = {
      request: {
        request_id: this.identity(),
        instance_id: this.session.instance_id,
        target,
        operation,
        expected_revision: revision,
        arguments: structuredClone(argumentsValue),
      },
      title,
      result: null,
      sending: true,
      unresolved: true,
      rejected: false,
      unavailable: false,
      error: null,
    };
    const retained = [...this.state.entries];
    if (retained.length >= 16) {
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

  retry = async (requestId: string): Promise<void> => {
    const entry = this.state.entries.find((item) => item.request.request_id === requestId);
    if (!entry || entry.sending || !entry.unresolved || !this.state.online || this.session?.instance_id !== entry.request.instance_id) {
      return;
    }
    this.update(requestId, { sending: true, error: null });
    await this.transmit(entry);
  };

  private async transmit(entry: Entry): Promise<void> {
    const session = this.session;
    if (session === null || session.instance_id !== entry.request.instance_id) return;
    try {
      const result = controlResult(
        await responseJson(
          await this.get("/api/controls", {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-Purrcept-Token": session.token },
            body: JSON.stringify(entry.request),
          }),
        ),
      );
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
