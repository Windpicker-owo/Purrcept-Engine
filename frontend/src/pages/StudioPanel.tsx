/**
 * 内嵌声线调音室与 Live2D 表演参数热更新工作台。
 * 直接复用统一 Web 服务的 /api/studio/* 与 /api/live2d/preferences，告别单独启动第二个终端命令。
 */
import { useEffect, useState, type ChangeEvent, type ReactElement } from "react";
import { Button, Feedback, Field, Switch } from "@purrcept/cel";
import type { PanelClient } from "../client";
import type { Live2DPreferencesData, Live2DPreferencesView, StudioStatus, VoiceProfileData } from "../model/admin";
import type { Route } from "../model/routes";
import { EmptyState, Section } from "../ui/common";

const DEFAULT_VOICE: VoiceProfileData = {
  version: 1,
  enabled: false,
  pitch_semitones: 0,
  highpass_hz: 0,
  low_db: 0,
  mid_db: 0,
  mid_hz: 1800,
  mid_q: 0.8,
  high_db: 0,
  gain_db: 0,
  limiter: true,
};

function SliderField({
  label,
  value,
  min,
  max,
  step,
  unit = "",
  onChange,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  unit?: string;
  onChange: (next: number) => void;
}): ReactElement {
  return (
    <div className="range-row">
      <div className="range-header">
        <span>{label}</span>
        <span className="identity">
          {value}
          {unit}
        </span>
      </div>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        aria-label={label}
        onChange={(e) => onChange(Number(e.target.value))}
      />
    </div>
  );
}

export function VoiceStudioSection({ client }: { client: PanelClient }): ReactElement {
  const [status, setStatus] = useState<StudioStatus | null>(null);
  const [profile, setProfile] = useState<VoiceProfileData>(DEFAULT_VOICE);
  const [revision, setRevision] = useState<string>("missing");
  const [clipId, setClipId] = useState<string | null>(null);
  const [durationSec, setDurationSec] = useState<number>(0);
  const [origUrl, setOrigUrl] = useState<string | null>(null);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    const init = async (): Promise<void> => {
      try {
        const [st, env] = await Promise.all([client.studioStatus(), client.loadVoiceProfile()]);
        if (!active) return;
        setStatus(st);
        setProfile(env.profile);
        setRevision(env.revision);
        if (st.voice.clip_id) {
          setClipId(st.voice.clip_id);
          setDurationSec(st.voice.duration);
          const blob = await client.voiceOriginalBlob(st.voice.clip_id);
          if (active) setOrigUrl(URL.createObjectURL(blob));
        }
      } catch (err: unknown) {
        if (active) setError(err instanceof Error ? err.message : "加载调音室状态失败。");
      }
    };
    void init();
    return () => {
      active = false;
    };
  }, [client]);

  useEffect(
    () => () => {
      if (origUrl) URL.revokeObjectURL(origUrl);
    },
    [origUrl],
  );
  useEffect(
    () => () => {
      if (previewUrl) URL.revokeObjectURL(previewUrl);
    },
    [previewUrl],
  );

  const handleUpload = async (event: ChangeEvent<HTMLInputElement>): Promise<void> => {
    const file = event.target.files?.[0];
    if (!file) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const buf = await file.arrayBuffer();
      const uploaded = await client.uploadVoiceSource(buf);
      setClipId(uploaded.clip_id);
      setDurationSec(uploaded.duration);
      const blob = await client.voiceOriginalBlob(uploaded.clip_id);
      setOrigUrl(URL.createObjectURL(blob));
      setPreviewUrl(null);
      setNotice(`样本已载入（${uploaded.duration.toFixed(1)} 秒），可调整下方参数并点击「生成调音试听」。`);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "上传样本解码失败，请确认本机已安装 ffmpeg 且音频为 WAV/MP3/FLAC。");
    } finally {
      setBusy(false);
      event.target.value = "";
    }
  };

  const handleRenderPreview = async (): Promise<void> => {
    if (!clipId || busy) return;
    setBusy(true);
    setError(null);
    try {
      const blob = await client.renderVoicePreviewBlob(clipId, profile);
      setPreviewUrl(URL.createObjectURL(blob));
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "试听渲染失败。");
    } finally {
      setBusy(false);
    }
  };

  const handleSave = async (): Promise<void> => {
    if (busy) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const saved = await client.saveVoiceProfile(revision, profile);
      setProfile(saved.profile);
      setRevision(saved.revision);
      setNotice(`声线预设已写入 ${status?.voice.profile_name ?? "voice-profile.json"}，后续语音输出将立即采用新参数。`);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "保存声线预设失败。");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="studio-stack">
      {error ? (
        <Feedback state="failed" scope="region" urgency="passive" persistence="retained" title="调音室提示">
          {error}
        </Feedback>
      ) : null}
      {notice ? (
        <Feedback state="completed" scope="region" urgency="passive" persistence="transient" onDismiss={() => setNotice(null)} title={notice} />
      ) : null}

      <Section
        id="voice-sample"
        title="1. 上传试听干声样本"
        aside={
          status ? (
            <span className="section-count">
              目标预设：{status.voice.profile_name} · {status.voice.sample_rate} Hz
            </span>
          ) : undefined
        }
      >
        <p className="muted">支持 90 秒以内、20 MiB 以内的 WAV、MP3 或 FLAC 干声样本；仅保存在当前服务内存中用于对比试听。</p>
        <div className="studio-grid">
          <Field label="选择本地音频样本">
            {(control) => <input {...control} type="file" accept=".wav,.mp3,.flac,audio/*" onChange={(e) => void handleUpload(e)} />}
          </Field>
          {origUrl ? (
            <div className="review-media">
              <span className="eyebrow">原声参考（{durationSec.toFixed(1)} 秒）</span>
              <audio controls src={origUrl} />
            </div>
          ) : null}
          {previewUrl ? (
            <div className="review-media">
              <span className="eyebrow">调音后试听结果</span>
              <audio controls src={previewUrl} />
            </div>
          ) : null}
        </div>
      </Section>

      <Section
        id="voice-dsp"
        title="2. 声线均衡与音高后处理（FFmpeg DSP）"
        aside={
          <div className="actions">
            <Button variant="secondary" onClick={() => setProfile(DEFAULT_VOICE)}>
              恢复默认旁通
            </Button>
            <Button
              variant="secondary"
              loading={busy}
              disabledReason={!clipId ? "请先在上方上传一段试听音频样本。" : undefined}
              onClick={() => void handleRenderPreview()}
            >
              生成调音试听
            </Button>
            <Button loading={busy} onClick={() => void handleSave()}>
              保存声线预设
            </Button>
          </div>
        }
      >
        <div className="setting-row">
          <div>
            <h3>启用声线后处理滤镜</h3>
            <p className="muted">开启后按下方半音、高通与三段均衡处理 TTS 输出音频；关闭则原样旁通。</p>
          </div>
          <Switch
            label="启用声线后处理滤镜"
            checked={profile.enabled}
            onCheckedChange={(enabled) => setProfile({ ...profile, enabled })}
          />
        </div>
        <div className="setting-row">
          <div>
            <h3>防爆音限幅器（Limiter）</h3>
            <p className="muted">提升增益或均衡后自动抑制峰值削波，建议保持开启。</p>
          </div>
          <Switch
            label="防爆音限幅器"
            checked={profile.limiter}
            onCheckedChange={(limiter) => setProfile({ ...profile, limiter })}
          />
        </div>

        <div className="studio-grid">
          <SliderField
            label="音高偏移（保持原语速）"
            value={profile.pitch_semitones}
            min={-3}
            max={3}
            step={0.1}
            unit=" 半音"
            onChange={(pitch_semitones) => setProfile({ ...profile, pitch_semitones })}
          />
          <SliderField
            label="高通滤波（去除低频浑浊）"
            value={profile.highpass_hz}
            min={0}
            max={200}
            step={5}
            unit=" Hz"
            onChange={(highpass_hz) => setProfile({ ...profile, highpass_hz })}
          />
          <SliderField
            label="低频增益（200 Hz）"
            value={profile.low_db}
            min={-9}
            max={9}
            step={0.5}
            unit=" dB"
            onChange={(low_db) => setProfile({ ...profile, low_db })}
          />
          <SliderField
            label="中频增益（人声主体厚度）"
            value={profile.mid_db}
            min={-9}
            max={9}
            step={0.5}
            unit=" dB"
            onChange={(mid_db) => setProfile({ ...profile, mid_db })}
          />
          <SliderField
            label="中频中心频率"
            value={profile.mid_hz}
            min={300}
            max={5000}
            step={50}
            unit=" Hz"
            onChange={(mid_hz) => setProfile({ ...profile, mid_hz })}
          />
          <SliderField
            label="中频带宽 Q 值"
            value={profile.mid_q}
            min={0.3}
            max={3}
            step={0.1}
            onChange={(mid_q) => setProfile({ ...profile, mid_q })}
          />
          <SliderField
            label="高频增益（空气感与齿音）"
            value={profile.high_db}
            min={-9}
            max={9}
            step={0.5}
            unit=" dB"
            onChange={(high_db) => setProfile({ ...profile, high_db })}
          />
          <SliderField
            label="整体输出增益"
            value={profile.gain_db}
            min={-18}
            max={6}
            step={0.5}
            unit=" dB"
            onChange={(gain_db) => setProfile({ ...profile, gain_db })}
          />
        </div>
      </Section>
    </div>
  );
}

export function Live2DStudioSection({
  client,
  go,
}: {
  client: PanelClient;
  go: (route: Route) => void;
}): ReactElement {
  const [view, setView] = useState<Live2DPreferencesView | null>(null);
  const [prefs, setPrefs] = useState<Live2DPreferencesData | null>(null);
  const [revision, setRevision] = useState<string>("missing");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = async (): Promise<void> => {
    try {
      const res = await client.loadLive2DPreferences();
      setView(res);
      if (res.available) {
        setPrefs(res.preferences);
        setRevision(res.revision);
      }
      setError(null);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "读取 Live2D 表演偏好失败。");
    }
  };

  useEffect(() => {
    void load();
  }, [client]);

  const handleSave = async (): Promise<void> => {
    if (!prefs || saving) return;
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const res = await client.saveLive2DPreferences(revision, prefs);
      setView(res);
      if (res.available) {
        setPrefs(res.preferences);
        setRevision(res.revision);
      }
      setNotice("Live2D 表演偏好已保存，正在运行的身体控制器会自动热重载，无需重启 Engine。");
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "保存 Live2D 表演偏好失败。");
    } finally {
      setSaving(false);
    }
  };

  if (view && !view.available) {
    return (
      <EmptyState
        icon="device"
        title="尚未配置已校准的 Live2D 模型"
        action={
          <Button onClick={() => go({ page: "system", id: null })}>
            前往设置添加或检查 live2d 插件
          </Button>
        }
      >
        {view.message}
      </EmptyState>
    );
  }

  if (!prefs) {
    return <p className="muted">正在读取 Live2D 模型偏好…</p>;
  }

  return (
    <div className="studio-stack">
      {error ? (
        <Feedback state="failed" scope="region" urgency="passive" persistence="retained" title="Live2D 参数提示">
          {error}
        </Feedback>
      ) : null}
      {notice ? (
        <Feedback state="completed" scope="region" urgency="passive" persistence="transient" onDismiss={() => setNotice(null)} title={notice} />
      ) : null}

      <Section
        id="live2d-posture"
        title={`Live2D 姿态与待机呼吸（模型：${prefs.model_id}）`}
        aside={
          <div className="actions">
            <Button variant="secondary" onClick={() => void load()}>
              重新读取
            </Button>
            <Button loading={saving} onClick={() => void handleSave()}>
              保存并热更新
            </Button>
          </div>
        }
      >
        <div className="setting-row">
          <div>
            <h3>启用空闲自然微动</h3>
            <p className="muted">没有显式动作指令时自动产生轻微呼吸、视线游移与头部微调。</p>
          </div>
          <Switch
            label="启用空闲自然微动"
            checked={prefs.idle_enabled}
            onCheckedChange={(idle_enabled) => setPrefs({ ...prefs, idle_enabled })}
          />
        </div>
        <div className="studio-grid">
          <SliderField
            label="待机停留平均间隔"
            value={prefs.idle_interval_seconds}
            min={1}
            max={12}
            step={0.2}
            unit=" 秒"
            onChange={(idle_interval_seconds) => setPrefs({ ...prefs, idle_interval_seconds })}
          />
          <SliderField
            label="空闲视线游移幅度"
            value={prefs.idle_gaze_scale}
            min={0}
            max={2}
            step={0.05}
            onChange={(idle_gaze_scale) => setPrefs({ ...prefs, idle_gaze_scale })}
          />
          <SliderField
            label="空闲姿态微动幅度"
            value={prefs.idle_posture_scale}
            min={0}
            max={2}
            step={0.05}
            onChange={(idle_posture_scale) => setPrefs({ ...prefs, idle_posture_scale })}
          />
          <SliderField
            label="默认睁眼程度"
            value={prefs.eye_open}
            min={0.2}
            max={1}
            step={0.02}
            onChange={(eye_open) => setPrefs({ ...prefs, eye_open })}
          />
          <SliderField
            label="头部基准左右偏移 X"
            value={prefs.head_x}
            min={-0.6}
            max={0.6}
            step={0.02}
            onChange={(head_x) => setPrefs({ ...prefs, head_x })}
          />
          <SliderField
            label="头部基准俯仰偏移 Y"
            value={prefs.head_y}
            min={-0.6}
            max={0.6}
            step={0.02}
            onChange={(head_y) => setPrefs({ ...prefs, head_y })}
          />
          <SliderField
            label="头部基准歪头偏移 Z"
            value={prefs.head_z}
            min={-0.6}
            max={0.6}
            step={0.02}
            onChange={(head_z) => setPrefs({ ...prefs, head_z })}
          />
          <SliderField
            label="身体基准左右偏移 X"
            value={prefs.body_x}
            min={-0.5}
            max={0.5}
            step={0.02}
            onChange={(body_x) => setPrefs({ ...prefs, body_x })}
          />
        </div>
      </Section>

      <Section id="live2d-mouth" title="语音口型同步灵敏度">
        <div className="studio-grid">
          <SliderField
            label="张嘴整体增益（Mouth Gain）"
            value={prefs.mouth_gain}
            min={0}
            max={4}
            step={0.05}
            onChange={(mouth_gain) => setPrefs({ ...prefs, mouth_gain })}
          />
          <SliderField
            label="口型曲线非线性 Gamma"
            value={prefs.mouth_gamma}
            min={0.4}
            max={2}
            step={0.05}
            onChange={(mouth_gamma) => setPrefs({ ...prefs, mouth_gamma })}
          />
          <SliderField
            label="嘴角基础笑意偏移（Mouth Form）"
            value={prefs.mouth_form}
            min={-1}
            max={1}
            step={0.05}
            onChange={(mouth_form) => setPrefs({ ...prefs, mouth_form })}
          />
          <SliderField
            label="开嘴响应时间（Attack）"
            value={prefs.mouth_attack_seconds}
            min={0.02}
            max={0.3}
            step={0.01}
            unit=" 秒"
            onChange={(mouth_attack_seconds) => setPrefs({ ...prefs, mouth_attack_seconds })}
          />
          <SliderField
            label="闭嘴回落时间（Release）"
            value={prefs.mouth_release_seconds}
            min={0.03}
            max={0.4}
            step={0.01}
            unit=" 秒"
            onChange={(mouth_release_seconds) => setPrefs({ ...prefs, mouth_release_seconds })}
          />
          <SliderField
            label="短停顿保持时间（Gap Hold）"
            value={prefs.mouth_gap_hold_seconds}
            min={0}
            max={0.2}
            step={0.01}
            unit=" 秒"
            onChange={(mouth_gap_hold_seconds) => setPrefs({ ...prefs, mouth_gap_hold_seconds })}
          />
        </div>
      </Section>
    </div>
  );
}
