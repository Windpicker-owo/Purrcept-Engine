/**
 * 真后台管理接口的前端数据契约与运行时窄化校验。
 * 与后端 config.py / admin_api.py 严格对齐，不向组件暴露未校验的 unknown 字段。
 */
import { ContractError, jsonObject, type ObjectValue } from "../contracts";

export interface ChatInputItem {
  readonly input_id: string;
  readonly adapter_id: string;
  readonly conversation_id: string;
  readonly display_name: string | null;
  readonly text: string;
  readonly occurred_at: string;
  readonly received_at: string;
  readonly stage: string;
  readonly reason: string | null;
  readonly turn_id: string | null;
}

export interface ChatReplyItem {
  readonly call_id: string;
  readonly channel: string;
  readonly path: string;
  readonly state: string;
  readonly text: string;
  readonly truncated: boolean;
}

export interface ChatCallBrief {
  readonly call_id: string;
  readonly path: string;
  readonly state: string;
  readonly error: string | null;
}

export interface ChatActionItem {
  readonly action_id: string;
  readonly input_id: string | null;
  readonly turn_id: string | null;
  readonly state: string;
  readonly created_at: number;
  readonly finished_at: number | null;
  readonly error: string | null;
  readonly replies: readonly ChatReplyItem[];
  readonly calls: readonly ChatCallBrief[];
}

export interface ChatFeed {
  readonly inputs: readonly ChatInputItem[];
  readonly actions: readonly ChatActionItem[];
  readonly console_enabled: boolean;
  readonly character_name: string;
  readonly foreground_busy: boolean;
  readonly can_process: boolean;
}

export interface ConfigCharacter {
  name: string;
  one_line: string;
  persona: string;
  relationship: string;
  speech_style: string;
  notes: string;
}

export interface ConfigGeneral {
  data_dir: string;
  console: boolean;
  web_enabled: boolean;
  web_port: number;
  log_level: "DEBUG" | "INFO" | "WARNING" | "ERROR";
}

export interface ConfigSubject {
  foreground: string;
  attention: string;
  reflection: string;
  proactive_thinking: boolean;
  proactive_idle_seconds: number;
  behavior_review_enabled: boolean;
  character: ConfigCharacter;
}

export interface ConfigModelItem {
  name: string;
  kind: "generation" | "decision";
  model: string;
  api_base: string;
  api_key_hint: string;
  env_expr: string | null;
  has_api_key: boolean;
  context_window: number;
  max_output_tokens: number;
  input_limit: number;
  new_api_key?: string;
  clear_api_key?: boolean;
}

export interface ConfigPluginItem {
  instance_id: string;
  entry: string;
  enabled: boolean;
  config: ObjectValue;
}

export interface PluginCatalogItem {
  readonly name: string;
  readonly summary: string;
  readonly default_config: ObjectValue;
  readonly snippet: string;
}

export type ConfigView =
  | { readonly available: false; readonly message: string }
  | {
      readonly available: true;
      readonly config_name: string;
      readonly revision: string;
      readonly validation_error: string | null;
      readonly general: ConfigGeneral;
      readonly subject: ConfigSubject;
      readonly models: ConfigModelItem[];
      readonly plugins: ConfigPluginItem[];
      readonly plugin_catalog: readonly PluginCatalogItem[];
      readonly raw_toml: string;
      readonly saved?: boolean;
      readonly restart_required?: boolean;
      readonly backup_name?: string;
    };

export interface VisualSavePayload {
  readonly mode: "visual";
  readonly revision: string;
  readonly general: ConfigGeneral;
  readonly subject: ConfigSubject;
  readonly models: readonly {
    readonly name: string;
    readonly kind: "generation" | "decision";
    readonly model: string;
    readonly api_base: string;
    readonly api_key: string;
    readonly keep_existing_key: boolean;
    readonly context_window: number;
    readonly max_output_tokens: number;
    readonly input_limit: number;
  }[];
  readonly plugins: readonly ConfigPluginItem[];
}

export interface NotebookFileSummary {
  readonly path: string;
  readonly category: "journal" | "memory" | "skills" | "notes";
  readonly title: string;
  readonly description: string;
  readonly size_bytes: number;
  readonly updated_at: number;
}

export interface NotebookInstance {
  readonly instance_id: string;
  readonly root_dir: string;
  readonly exists: boolean;
  readonly files: readonly NotebookFileSummary[];
}

export interface NotebooksOverview {
  readonly available: boolean;
  readonly instances: readonly NotebookInstance[];
}

export interface NotebookFileContent {
  readonly instance: string;
  readonly path: string;
  readonly content: string;
  readonly revision: string;
  readonly updated_at: number;
}

export interface VoiceProfileData {
  version: number;
  enabled: boolean;
  pitch_semitones: number;
  highpass_hz: number;
  low_db: number;
  mid_db: number;
  mid_hz: number;
  mid_q: number;
  high_db: number;
  gain_db: number;
  limiter: boolean;
}

export interface VoiceProfileEnvelope {
  readonly profile: VoiceProfileData;
  readonly revision: string;
}

export interface StudioStatus {
  readonly voice: {
    readonly profile_name: string;
    readonly sample_rate: number;
    readonly clip_id: string | null;
    readonly duration: number;
  };
  readonly live2d_available: boolean;
}

export interface Live2DPreferencesData {
  model_id: string;
  idle_enabled: boolean;
  idle_interval_seconds: number;
  idle_gaze_scale: number;
  idle_posture_scale: number;
  head_x: number;
  head_y: number;
  head_z: number;
  body_x: number;
  body_y: number;
  body_z: number;
  gaze_x: number;
  gaze_y: number;
  eye_open: number;
  mouth_gain: number;
  mouth_gamma: number;
  mouth_form: number;
  mouth_form_gain: number;
  mouth_viseme_form_scale: number;
  mouth_attack_seconds: number;
  mouth_release_seconds: number;
  mouth_gap_hold_seconds: number;
}

export type Live2DPreferencesView =
  | { readonly available: false; readonly message: string }
  | {
      readonly available: true;
      readonly model_id: string;
      readonly preferences: Live2DPreferencesData;
      readonly revision: string;
      readonly saved?: boolean;
    };

function record(value: unknown, label: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new ContractError(label);
  }
  return value as Record<string, unknown>;
}

function str(value: unknown, label: string): string {
  if (typeof value !== "string") throw new ContractError(label);
  return value;
}

function optStr(value: unknown, label: string): string | null {
  if (value === null || value === undefined) return null;
  return str(value, label);
}

function bool(value: unknown, label: string): boolean {
  if (typeof value !== "boolean") throw new ContractError(label);
  return value;
}

function num(value: unknown, label: string): number {
  if (typeof value !== "number" || !Number.isFinite(value)) throw new ContractError(label);
  return value;
}

function previewError(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  if (typeof value === "string") return value;
  if (typeof value === "object" && !Array.isArray(value) && "text" in value && typeof value.text === "string") {
    return value.text;
  }
  return null;
}

export function parseChatFeed(raw: unknown): ChatFeed {
  const root = record(raw, "chat");
  const rawInputs = Array.isArray(root.inputs) ? root.inputs : [];
  const rawActions = Array.isArray(root.actions) ? root.actions : [];
  return {
    console_enabled: bool(root.console_enabled, "chat.console_enabled"),
    character_name: str(root.character_name, "chat.character_name"),
    foreground_busy: bool(root.foreground_busy, "chat.foreground_busy"),
    can_process: bool(root.can_process, "chat.can_process"),
    inputs: rawInputs.map((item, idx) => {
      const r = record(item, `chat.inputs[${idx}]`);
      return {
        input_id: str(r.input_id, "input_id"),
        adapter_id: str(r.adapter_id, "adapter_id"),
        conversation_id: str(r.conversation_id, "conversation_id"),
        display_name: optStr(r.display_name, "display_name"),
        text: str(r.text, "text"),
        occurred_at: str(r.occurred_at, "occurred_at"),
        received_at: str(r.received_at, "received_at"),
        stage: str(r.stage, "stage"),
        reason: optStr(r.reason, "reason"),
        turn_id: optStr(r.turn_id, "turn_id"),
      };
    }),
    actions: rawActions.map((item, idx) => {
      const r = record(item, `chat.actions[${idx}]`);
      const repliesRaw = Array.isArray(r.replies) ? r.replies : [];
      const callsRaw = Array.isArray(r.calls) ? r.calls : [];
      return {
        action_id: str(r.action_id, "action_id"),
        input_id: optStr(r.input_id, "input_id"),
        turn_id: optStr(r.turn_id, "turn_id"),
        state: str(r.state, "state"),
        created_at: num(r.created_at, "created_at"),
        finished_at: r.finished_at === null ? null : num(r.finished_at, "finished_at"),
        error: previewError(r.error),
        replies: repliesRaw.map((rep, rIdx) => {
          const rr = record(rep, `replies[${rIdx}]`);
          return {
            call_id: str(rr.call_id, "call_id"),
            channel: str(rr.channel, "channel"),
            path: str(rr.path, "path"),
            state: str(rr.state, "state"),
            text: str(rr.text, "text"),
            truncated: bool(rr.truncated, "truncated"),
          };
        }),
        calls: callsRaw.map((c, cIdx) => {
          const cr = record(c, `calls[${cIdx}]`);
          return {
            call_id: str(cr.call_id, "call_id"),
            path: str(cr.path, "path"),
            state: str(cr.state, "state"),
            error: previewError(cr.error),
          };
        }),
      };
    }),
  };
}

export function parseConfigView(raw: unknown): ConfigView {
  const root = record(raw, "config");
  if (!bool(root.available, "config.available")) {
    return { available: false, message: str(root.message, "config.message") };
  }
  const gen = record(root.general, "config.general");
  const subj = record(root.subject, "config.subject");
  const char = record(subj.character, "config.subject.character");
  const levelRaw = str(gen.log_level, "log_level");
  const logLevel: ConfigGeneral["log_level"] =
    levelRaw === "DEBUG" || levelRaw === "WARNING" || levelRaw === "ERROR" ? levelRaw : "INFO";
  const modelsArr = Array.isArray(root.models) ? root.models : [];
  const pluginsArr = Array.isArray(root.plugins) ? root.plugins : [];
  const catalogArr = Array.isArray(root.plugin_catalog) ? root.plugin_catalog : [];

  return {
    available: true,
    config_name: str(root.config_name, "config_name"),
    revision: str(root.revision, "revision"),
    validation_error: optStr(root.validation_error, "validation_error"),
    general: {
      data_dir: str(gen.data_dir, "data_dir"),
      console: bool(gen.console, "console"),
      web_enabled: bool(gen.web_enabled, "web_enabled"),
      web_port: num(gen.web_port, "web_port"),
      log_level: logLevel,
    },
    subject: {
      foreground: str(subj.foreground, "foreground"),
      attention: str(subj.attention, "attention"),
      reflection: str(subj.reflection, "reflection"),
      proactive_thinking: bool(subj.proactive_thinking, "proactive_thinking"),
      proactive_idle_seconds: num(subj.proactive_idle_seconds, "proactive_idle_seconds"),
      behavior_review_enabled: bool(subj.behavior_review_enabled, "behavior_review_enabled"),
      character: {
        name: str(char.name, "name"),
        one_line: str(char.one_line, "one_line"),
        persona: str(char.persona, "persona"),
        relationship: str(char.relationship, "relationship"),
        speech_style: str(char.speech_style, "speech_style"),
        notes: str(char.notes, "notes"),
      },
    },
    models: modelsArr.map((m, idx) => {
      const mr = record(m, `models[${idx}]`);
      return {
        name: str(mr.name, "model.name"),
        kind: mr.kind === "decision" ? "decision" : "generation",
        model: str(mr.model, "model.model"),
        api_base: str(mr.api_base, "model.api_base"),
        api_key_hint: str(mr.api_key_hint, "model.api_key_hint"),
        env_expr: optStr(mr.env_expr, "model.env_expr"),
        has_api_key: bool(mr.has_api_key, "model.has_api_key"),
        context_window: num(mr.context_window, "model.context_window"),
        max_output_tokens: num(mr.max_output_tokens, "model.max_output_tokens"),
        input_limit: num(mr.input_limit, "model.input_limit"),
      };
    }),
    plugins: pluginsArr.map((p, idx) => {
      const pr = record(p, `plugins[${idx}]`);
      return {
        instance_id: str(pr.instance_id, "plugin.instance_id"),
        entry: str(pr.entry, "plugin.entry"),
        enabled: bool(pr.enabled, "plugin.enabled"),
        config: jsonObject(pr.config ?? {}, "plugin.config"),
      };
    }),
    plugin_catalog: catalogArr.map((c, idx) => {
      const cr = record(c, `plugin_catalog[${idx}]`);
      return {
        name: str(cr.name, "catalog.name"),
        summary: str(cr.summary, "catalog.summary"),
        default_config: jsonObject(cr.default_config ?? {}, "catalog.default_config"),
        snippet: str(cr.snippet, "catalog.snippet"),
      };
    }),
    raw_toml: str(root.raw_toml, "raw_toml"),
    saved: typeof root.saved === "boolean" ? root.saved : undefined,
    restart_required: typeof root.restart_required === "boolean" ? root.restart_required : undefined,
    backup_name: typeof root.backup_name === "string" ? root.backup_name : undefined,
  };
}

export function parseNotebooksOverview(raw: unknown): NotebooksOverview {
  const root = record(raw, "notebooks");
  const list = Array.isArray(root.instances) ? root.instances : [];
  return {
    available: bool(root.available, "notebooks.available"),
    instances: list.map((inst, idx) => {
      const ir = record(inst, `notebooks.instances[${idx}]`);
      const files = Array.isArray(ir.files) ? ir.files : [];
      return {
        instance_id: str(ir.instance_id, "instance_id"),
        root_dir: str(ir.root_dir, "root_dir"),
        exists: bool(ir.exists, "exists"),
        files: files.map((f, fIdx) => {
          const fr = record(f, `files[${fIdx}]`);
          const catRaw = str(fr.category, "category");
          const category: NotebookFileSummary["category"] =
            catRaw === "journal" || catRaw === "memory" || catRaw === "skills" ? catRaw : "notes";
          return {
            path: str(fr.path, "path"),
            category,
            title: str(fr.title, "title"),
            description: str(fr.description, "description"),
            size_bytes: num(fr.size_bytes, "size_bytes"),
            updated_at: num(fr.updated_at, "updated_at"),
          };
        }),
      };
    }),
  };
}

export function parseNotebookFile(raw: unknown): NotebookFileContent {
  const r = record(raw, "notebook_file");
  return {
    instance: str(r.instance, "instance"),
    path: str(r.path, "path"),
    content: str(r.content, "content"),
    revision: str(r.revision, "revision"),
    updated_at: num(r.updated_at, "updated_at"),
  };
}

export function parseStudioStatus(raw: unknown): StudioStatus {
  const r = record(raw, "studio_status");
  const v = record(r.voice, "studio_status.voice");
  return {
    voice: {
      profile_name: str(v.profile_name, "profile_name"),
      sample_rate: num(v.sample_rate, "sample_rate"),
      clip_id: optStr(v.clip_id, "clip_id"),
      duration: num(v.duration, "duration"),
    },
    live2d_available: bool(r.live2d_available, "live2d_available"),
  };
}

export function parseVoiceProfileEnvelope(raw: unknown): VoiceProfileEnvelope {
  const r = record(raw, "voice_profile");
  const p = record(r.profile, "voice_profile.profile");
  return {
    revision: str(r.revision, "revision"),
    profile: {
      version: num(p.version, "version"),
      enabled: bool(p.enabled, "enabled"),
      pitch_semitones: num(p.pitch_semitones, "pitch_semitones"),
      highpass_hz: num(p.highpass_hz, "highpass_hz"),
      low_db: num(p.low_db, "low_db"),
      mid_db: num(p.mid_db, "mid_db"),
      mid_hz: num(p.mid_hz, "mid_hz"),
      mid_q: num(p.mid_q, "mid_q"),
      high_db: num(p.high_db, "high_db"),
      gain_db: num(p.gain_db, "gain_db"),
      limiter: bool(p.limiter, "limiter"),
    },
  };
}

export function parseLive2DPreferencesView(raw: unknown): Live2DPreferencesView {
  const r = record(raw, "live2d_preferences");
  if (!bool(r.available, "available")) {
    return { available: false, message: str(r.message, "message") };
  }
  const p = record(r.preferences, "preferences");
  return {
    available: true,
    model_id: str(r.model_id, "model_id"),
    revision: str(r.revision, "revision"),
    saved: typeof r.saved === "boolean" ? r.saved : undefined,
    preferences: {
      model_id: str(p.model_id, "model_id"),
      idle_enabled: bool(p.idle_enabled, "idle_enabled"),
      idle_interval_seconds: num(p.idle_interval_seconds, "idle_interval_seconds"),
      idle_gaze_scale: num(p.idle_gaze_scale, "idle_gaze_scale"),
      idle_posture_scale: num(p.idle_posture_scale, "idle_posture_scale"),
      head_x: num(p.head_x, "head_x"),
      head_y: num(p.head_y, "head_y"),
      head_z: num(p.head_z, "head_z"),
      body_x: num(p.body_x, "body_x"),
      body_y: num(p.body_y, "body_y"),
      body_z: num(p.body_z, "body_z"),
      gaze_x: num(p.gaze_x, "gaze_x"),
      gaze_y: num(p.gaze_y, "gaze_y"),
      eye_open: num(p.eye_open, "eye_open"),
      mouth_gain: num(p.mouth_gain, "mouth_gain"),
      mouth_gamma: num(p.mouth_gamma, "mouth_gamma"),
      mouth_form: num(p.mouth_form, "mouth_form"),
      mouth_form_gain: num(p.mouth_form_gain, "mouth_form_gain"),
      mouth_viseme_form_scale: num(p.mouth_viseme_form_scale, "mouth_viseme_form_scale"),
      mouth_attack_seconds: num(p.mouth_attack_seconds, "mouth_attack_seconds"),
      mouth_release_seconds: num(p.mouth_release_seconds, "mouth_release_seconds"),
      mouth_gap_hold_seconds: num(p.mouth_gap_hold_seconds, "mouth_gap_hold_seconds"),
    },
  };
}
