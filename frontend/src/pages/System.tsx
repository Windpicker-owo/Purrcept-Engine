/**
 * 配置中心与系统健康：可视化编辑人设、模型、插件并无损写回 purrcept.toml，同时收纳精简后的运行健康状态。
 */
import { useEffect, useState, type FormEvent, type ReactElement } from "react";
import { Button, Feedback, Field, Switch } from "@purrcept/cel";
import type { PanelClient, PanelState } from "../client";
import { jsonObject } from "../contracts";
import type {
  ConfigGeneral,
  ConfigModelItem,
  ConfigPluginItem,
  ConfigSubject,
  ConfigView,
  PluginCatalogItem,
} from "../model/admin";
import { targetReason } from "../model/controls";
import type { Route } from "../model/routes";
import { mark } from "../semantics";
import { duration, EmptyNote, PageHeading, Section, StateMark, TechDetails } from "../ui/common";

type SettingsTab = "persona" | "models" | "raw";

const COMPONENT_LABELS: Readonly<Record<string, string>> = {
  startup: "启动引导",
  storage: "本地数据库",
  input_handoff: "输入调度",
  maintenance: "后台维护",
  foreground: "主脑循环",
  background: "后台记忆整理",
  behavior_analysis: "反思改进",
};

export function System({
  state,
  client,
  go,
}: {
  state: PanelState;
  client: PanelClient;
  go: (route: Route) => void;
}): ReactElement {
  const [tab, setTab] = useState<SettingsTab>("persona");
  const [configView, setConfigView] = useState<ConfigView | null>(null);
  const [general, setGeneral] = useState<ConfigGeneral | null>(null);
  const [subject, setSubject] = useState<ConfigSubject | null>(null);
  const [models, setModels] = useState<ConfigModelItem[]>([]);
  const [plugins, setPlugins] = useState<ConfigPluginItem[]>([]);
  const [pluginJsonDrafts, setPluginJsonDrafts] = useState<Record<number, string>>({});
  const [rawToml, setRawToml] = useState("");
  const [selectedCatalog, setSelectedCatalog] = useState("");
  const [saving, setSaving] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const hydrate = (res: ConfigView): void => {
    setConfigView(res);
    if (!res.available) return;
    setGeneral(structuredClone(res.general));
    setSubject(structuredClone(res.subject));
    setModels(structuredClone(res.models));
    setPlugins(structuredClone(res.plugins));
    const drafts: Record<number, string> = {};
    res.plugins.forEach((p, idx) => {
      drafts[idx] = JSON.stringify(p.config, null, 2);
    });
    setPluginJsonDrafts(drafts);
    setRawToml(res.raw_toml);
  };

  const load = async (): Promise<void> => {
    setLoading(true);
    try {
      const res = await client.loadConfig();
      hydrate(res);
      setError(null);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "读取 purrcept.toml 失败。");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, [client]);

  const handleVisualSave = async (event?: FormEvent): Promise<void> => {
    event?.preventDefault();
    if (!configView || !configView.available || !general || !subject || saving) return;
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const parsedPlugins: ConfigPluginItem[] = plugins.map((p, idx) => {
        const rawText = (pluginJsonDrafts[idx] ?? "{}").trim() || "{}";
        let parsedCfg: unknown;
        try {
          parsedCfg = JSON.parse(rawText);
        } catch {
          throw new Error(`插件 "${p.instance_id}" 的配置 JSON 语法有误，请检查括号与引号。`);
        }
        return {
          ...p,
          config: jsonObject(parsedCfg, `plugins.${p.instance_id}.config`),
        };
      });

      const res = await client.saveVisualConfig({
        mode: "visual",
        revision: configView.revision,
        general,
        subject,
        models: models.map((m) => ({
          name: m.name.trim(),
          kind: m.kind,
          model: m.model.trim(),
          api_base: m.api_base.trim(),
          api_key: m.clear_api_key ? "" : (m.new_api_key ?? "").trim(),
          keep_existing_key: !m.clear_api_key && !(m.new_api_key ?? "").trim(),
          context_window: m.context_window,
          max_output_tokens: m.max_output_tokens,
          input_limit: m.input_limit,
        })),
        plugins: parsedPlugins,
      });
      hydrate(res);
      setNotice(
        `配置已校验并写回 ${res.available ? res.config_name : "purrcept.toml"}（已自动滚动备份至 config-backups/）。模型、人设与插件改动将在下次重启 Engine 后生效。`,
      );
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "保存配置失败。");
    } finally {
      setSaving(false);
    }
  };

  const handleRawSave = async (): Promise<void> => {
    if (!configView || !configView.available || saving) return;
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const res = await client.saveRawConfig(configView.revision, rawToml);
      hydrate(res);
      setNotice("TOML 源码已校验通过并保存（未改动的 ******** 密钥已自动还原为原密钥）。重启 Engine 后生效。");
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "保存 TOML 源码失败。");
    } finally {
      setSaving(false);
    }
  };

  const addModel = (): void => {
    const idx = models.length + 1;
    setModels([
      ...models,
      {
        name: `model_${idx}`,
        kind: "generation",
        model: "openai/gpt-4.1-mini",
        api_base: "https://api.openai.com/v1",
        api_key_hint: "免密 / 未填写",
        env_expr: null,
        has_api_key: false,
        context_window: 64000,
        max_output_tokens: 4096,
        input_limit: 16000,
        new_api_key: "${OPENAI_API_KEY}",
      },
    ]);
  };

  const addPluginFromCatalog = (catalog: readonly PluginCatalogItem[]): void => {
    const chosen = catalog.find((c) => c.name === selectedCatalog) ?? catalog[0];
    if (!chosen) return;
    let instanceId = chosen.name;
    let suffix = 2;
    while (plugins.some((p) => p.instance_id === instanceId)) {
      instanceId = `${chosen.name}_${suffix++}`;
    }
    const nextIdx = plugins.length;
    const nextPlugin: ConfigPluginItem = {
      instance_id: instanceId,
      entry: chosen.name,
      enabled: true,
      config: structuredClone(chosen.default_config),
    };
    setPlugins([...plugins, nextPlugin]);
    setPluginJsonDrafts({
      ...pluginJsonDrafts,
      [nextIdx]: JSON.stringify(chosen.default_config, null, 2),
    });
  };

  const s = state.snapshot;
  const proactive = s?.proactive_thinking ?? null;
  const proactiveReason =
    targetReason(state, "subject", "set_proactive_thinking") ??
    (proactive === null ? "当前没有启用主脑。" : undefined);
  const failedComponents = s?.health.components.filter((item) => item.state === "failed") ?? [];

  return (
    <>
      <PageHeading
        title="配置、人设与系统运行"
        aside={
          <div className="actions">
            <Button variant="secondary" loading={loading} onClick={() => void load()}>
              重新读取配置
            </Button>
            <Button variant="ghost" onClick={() => go({ page: "devices", id: "studio" })}>
              打开声线调音室
            </Button>
          </div>
        }
      >
        直接可视化修改人设六要素、模型接口与插件并写回 purrcept.toml（保留文件注释且不泄露明文密钥），同时掌握核心运行健康。
      </PageHeading>

      {error ? (
        <Feedback state="failed" scope="region" urgency="passive" persistence="retained" title="配置校验或保存未通过">
          {error}
        </Feedback>
      ) : null}
      {notice ? (
        <Feedback state="completed" scope="region" urgency="passive" persistence="retained" onDismiss={() => setNotice(null)} title="保存成功">
          {notice}
        </Feedback>
      ) : null}

      <Section id="live-controls" title="即时生效开关（无需重启）">
        <div className="setting-row">
          <div>
            <h3>主动思考</h3>
            <p className="muted">
              没有新消息时，静置满 {proactive ? duration(proactive.idle_seconds) : "设定时长"} 后由主脑自主决定是否发起问候、写随笔或继续手头的事。
            </p>
          </div>
          <Switch
            label="主动思考"
            checked={proactive ? proactive.enabled : null}
            disabledReason={proactiveReason}
            onCheckedChange={(enabled) => {
              if (proactive) {
                void client.submit(
                  "subject",
                  "set_proactive_thinking",
                  proactive.revision,
                  { enabled },
                  enabled ? "开启主动思考" : "关闭主动思考",
                );
              }
            }}
          />
        </div>
      </Section>

      {configView && !configView.available ? (
        <Section id="config-unavailable" title="配置文件编辑">
          <EmptyNote icon="settings">{configView.message}</EmptyNote>
        </Section>
      ) : configView && configView.available && general && subject ? (
        <>
          <div className="timeline-tools">
            <fieldset className="toolbar">
              <legend className="visually-hidden">配置编辑分区</legend>
              {(
                [
                  ["persona", "人设与主脑"],
                  ["models", "模型与插件"],
                  ["raw", "TOML 源码与健康"],
                ] as const
              ).map(([key, label]) => (
                <label key={key} className="filter" data-selected={tab === key}>
                  <input type="radio" name="settings-tab" checked={tab === key} onChange={() => setTab(key)} />
                  {label}
                </label>
              ))}
            </fieldset>
          </div>

          {tab === "persona" ? (
            <form className="page" onSubmit={(e) => void handleVisualSave(e)}>
              <Section
                id="persona-editor"
                title={`角色身份卡片（[subject.character] · ${configView.config_name}）`}
                aside={
                  <Button type="submit" loading={saving}>
                    保存到 purrcept.toml
                  </Button>
                }
              >
                <div className="config-grid">
                  <Field label="角色名字（name）" description="例如：杰瑞喵 / 小咕">
                    {(control) => (
                      <input
                        {...control}
                        type="text"
                        value={subject.character.name}
                        onChange={(e) => setSubject({ ...subject, character: { ...subject.character, name: e.target.value } })}
                      />
                    )}
                  </Field>
                  <Field label="一句话简介（one_line）" description="浓缩概括她是谁、住在怎样的环境里">
                    {(control) => (
                      <input
                        {...control}
                        type="text"
                        value={subject.character.one_line}
                        onChange={(e) => setSubject({ ...subject, character: { ...subject.character, one_line: e.target.value } })}
                      />
                    )}
                  </Field>
                </div>
                <div className="config-grid">
                  <Field label="性格与行事习惯（persona）">
                    {(control) => (
                      <textarea
                        {...control}
                        rows={3}
                        value={subject.character.persona}
                        onChange={(e) => setSubject({ ...subject, character: { ...subject.character, persona: e.target.value } })}
                      />
                    )}
                  </Field>
                  <Field label="与你的关系（relationship）">
                    {(control) => (
                      <textarea
                        {...control}
                        rows={3}
                        value={subject.character.relationship}
                        onChange={(e) => setSubject({ ...subject, character: { ...subject.character, relationship: e.target.value } })}
                      />
                    )}
                  </Field>
                  <Field label="说话语气与口癖（speech_style）">
                    {(control) => (
                      <textarea
                        {...control}
                        rows={3}
                        value={subject.character.speech_style}
                        onChange={(e) => setSubject({ ...subject, character: { ...subject.character, speech_style: e.target.value } })}
                      />
                    )}
                  </Field>
                  <Field label="补充设定与注意事项（notes）">
                    {(control) => (
                      <textarea
                        {...control}
                        rows={3}
                        value={subject.character.notes}
                        onChange={(e) => setSubject({ ...subject, character: { ...subject.character, notes: e.target.value } })}
                      />
                    )}
                  </Field>
                </div>
              </Section>

              <Section id="routing-editor" title="主脑模型分工与基础选项">
                <div className="config-grid">
                  <Field label="主脑生成模型（foreground）" description="负责思考并编写行动代码的核心生成模型">
                    {(control) => (
                      <select {...control} value={subject.foreground} onChange={(e) => setSubject({ ...subject, foreground: e.target.value })}>
                        {models
                          .filter((m) => m.kind === "generation")
                          .map((m) => (
                            <option key={m.name} value={m.name}>
                              {m.name}（{m.model}）
                            </option>
                          ))}
                      </select>
                    )}
                  </Field>
                  <Field label="反思复盘模型（reflection）" description="默认复用主脑生成模型即可">
                    {(control) => (
                      <select {...control} value={subject.reflection} onChange={(e) => setSubject({ ...subject, reflection: e.target.value })}>
                        {models
                          .filter((m) => m.kind === "generation")
                          .map((m) => (
                            <option key={m.name} value={m.name}>
                              {m.name}（{m.model}）
                            </option>
                          ))}
                      </select>
                    )}
                  </Field>
                  <Field label="注意门控模型（attention，可选）" description="留空或复用主脑模型均可开箱即用">
                    {(control) => (
                      <select {...control} value={subject.attention} onChange={(e) => setSubject({ ...subject, attention: e.target.value })}>
                        <option value="">不单独指定（直接唤醒主脑）</option>
                        {models.map((m) => (
                          <option key={m.name} value={m.name}>
                            {m.name}（{m.kind} · {m.model}）
                          </option>
                        ))}
                      </select>
                    )}
                  </Field>
                  <Field label="日志输出级别（logging.level）">
                    {(control) => (
                      <select
                        {...control}
                        value={general.log_level}
                        onChange={(e) =>
                          setGeneral({
                            ...general,
                            log_level: e.target.value as ConfigGeneral["log_level"],
                          })
                        }
                      >
                        <option value="DEBUG">DEBUG（含详细调试堆栈）</option>
                        <option value="INFO">INFO（推荐：清晰显示生命周期动态）</option>
                        <option value="WARNING">WARNING（仅警告与错误）</option>
                        <option value="ERROR">ERROR（仅报错）</option>
                      </select>
                    )}
                  </Field>
                  <Field label="主动思考静置间隔（秒）">
                    {(control) => (
                      <input
                        {...control}
                        type="number"
                        min={10}
                        step={10}
                        value={subject.proactive_idle_seconds}
                        onChange={(e) => setSubject({ ...subject, proactive_idle_seconds: Math.max(1, Number(e.target.value) || 300) })}
                      />
                    )}
                  </Field>
                  <Field label="Web 管理后台端口（web.port）">
                    {(control) => (
                      <input
                        {...control}
                        type="number"
                        min={1}
                        max={65535}
                        value={general.web_port}
                        onChange={(e) => setGeneral({ ...general, web_port: Number(e.target.value) || 8765 })}
                      />
                    )}
                  </Field>
                </div>

                <div className="setting-row">
                  <div>
                    <h3>启用控制台与网页文字回复（console）</h3>
                    <p className="muted">开启后挂载 me.console.output，让主脑能直接在终端和本网页「对话」页发出文字回复。</p>
                  </div>
                  <Switch
                    label="启用控制台与网页文字回复"
                    checked={general.console}
                    onCheckedChange={(consoleVal) => setGeneral({ ...general, console: consoleVal })}
                  />
                </div>
                <div className="setting-row">
                  <div>
                    <h3>下次启动默认开启主动思考</h3>
                    <p className="muted">持久化写入 subject.proactive_thinking。</p>
                  </div>
                  <Switch
                    label="下次启动默认开启主动思考"
                    checked={subject.proactive_thinking}
                    onCheckedChange={(proactive_thinking) => setSubject({ ...subject, proactive_thinking })}
                  />
                </div>
                <div className="actions">
                  <Button type="submit" loading={saving}>
                    保存全部改动到 purrcept.toml
                  </Button>
                </div>
              </Section>
            </form>
          ) : tab === "models" ? (
            <form className="page" onSubmit={(e) => void handleVisualSave(e)}>
              <Section
                id="models-editor"
                title="模型接口列表（[models.*]）"
                aside={
                  <div className="actions">
                    <Button variant="secondary" onClick={addModel}>
                      新增模型条目
                    </Button>
                    <Button type="submit" loading={saving}>
                      保存到 purrcept.toml
                    </Button>
                  </div>
                }
              >
                <p className="muted">
                  支持直接填写 <code>{"${OPENAI_API_KEY}"}</code> 环境变量引用；本机 Ollama / vLLM / LM Studio（127.0.0.1 或局域网地址）可留空免密。不填写新密钥时自动保留磁盘原密钥。
                </p>
                <div className="studio-stack">
                  {models.map((m, idx) => (
                    <article key={idx} className="config-card">
                      <div className="object-header">
                        <h3>
                          模型 #{idx + 1}：{m.name}
                        </h3>
                        {models.length > 1 ? (
                          <Button variant="ghost" onClick={() => setModels(models.filter((_, i) => i !== idx))}>
                            移除此模型
                          </Button>
                        ) : null}
                      </div>
                      <div className="config-grid">
                        <Field label="配置别名（供 foreground 等引用）">
                          {(control) => (
                            <input
                              {...control}
                              type="text"
                              value={m.name}
                              onChange={(e) => {
                                const copy = [...models];
                                copy[idx] = { ...m, name: e.target.value };
                                setModels(copy);
                              }}
                            />
                          )}
                        </Field>
                        <Field label="模型角色种类（kind）">
                          {(control) => (
                            <select
                              {...control}
                              value={m.kind}
                              onChange={(e) => {
                                const copy = [...models];
                                copy[idx] = { ...m, kind: e.target.value === "decision" ? "decision" : "generation" };
                                setModels(copy);
                              }}
                            >
                              <option value="generation">generation（生成与行动）</option>
                              <option value="decision">decision（轻量注意判断）</option>
                            </select>
                          )}
                        </Field>
                        <Field label="模型标识（provider/model）">
                          {(control) => (
                            <input
                              {...control}
                              type="text"
                              placeholder="openai/gpt-4.1 或 openai/deepseek-chat"
                              value={m.model}
                              onChange={(e) => {
                                const copy = [...models];
                                copy[idx] = { ...m, model: e.target.value };
                                setModels(copy);
                              }}
                            />
                          )}
                        </Field>
                        <Field label="API Base 地址">
                          {(control) => (
                            <input
                              {...control}
                              type="text"
                              placeholder="https://api.openai.com/v1 或 http://127.0.0.1:11434/v1"
                              value={m.api_base}
                              onChange={(e) => {
                                const copy = [...models];
                                copy[idx] = { ...m, api_base: e.target.value };
                                setModels(copy);
                              }}
                            />
                          )}
                        </Field>
                        <Field label={`API Key（当前：${m.api_key_hint}）`} description="留空则保持原密钥不变；支持 ${ENV_VAR} 语法">
                          {(control) => (
                            <input
                              {...control}
                              type="password"
                              placeholder="留空保持原密钥不变，或输入新密钥 / ${ENV_VAR}"
                              value={m.new_api_key ?? ""}
                              onChange={(e) => {
                                const copy = [...models];
                                copy[idx] = { ...m, new_api_key: e.target.value, clear_api_key: false };
                                setModels(copy);
                              }}
                            />
                          )}
                        </Field>
                        {m.kind === "generation" ? (
                          <Field label="上下文窗口 / 最大输出 Token">
                            {(control) => (
                              <div className="inline-pair">
                                <input
                                  {...control}
                                  type="number"
                                  min={1024}
                                  value={m.context_window}
                                  onChange={(e) => {
                                    const copy = [...models];
                                    copy[idx] = { ...m, context_window: Number(e.target.value) || 64000 };
                                    setModels(copy);
                                  }}
                                />
                                <input
                                  type="number"
                                  aria-label="最大输出 Token"
                                  min={256}
                                  value={m.max_output_tokens}
                                  onChange={(e) => {
                                    const copy = [...models];
                                    copy[idx] = { ...m, max_output_tokens: Number(e.target.value) || 4096 };
                                    setModels(copy);
                                  }}
                                />
                              </div>
                            )}
                          </Field>
                        ) : (
                          <Field label="输入上限 Token（input_limit）">
                            {(control) => (
                              <input
                                {...control}
                                type="number"
                                min={512}
                                value={m.input_limit}
                                onChange={(e) => {
                                  const copy = [...models];
                                  copy[idx] = { ...m, input_limit: Number(e.target.value) || 16000 };
                                  setModels(copy);
                                }}
                              />
                            )}
                          </Field>
                        )}
                      </div>
                    </article>
                  ))}
                </div>
              </Section>

              <Section
                id="plugins-editor"
                title="插件装配表（[[plugins]]）"
                aside={
                  <Button type="submit" loading={saving}>
                    保存插件与模型配置
                  </Button>
                }
              >
                <div className="plugin-adder">
                  <Field label="从 11 个内置插件模板一键添加">
                    {(control) => (
                      <select
                        {...control}
                        value={selectedCatalog || configView.plugin_catalog[0]?.name || ""}
                        onChange={(e) => setSelectedCatalog(e.target.value)}
                      >
                        {configView.plugin_catalog.map((cat) => (
                          <option key={cat.name} value={cat.name}>
                            {cat.name} — {cat.summary}
                          </option>
                        ))}
                      </select>
                    )}
                  </Field>
                  <Button variant="secondary" onClick={() => addPluginFromCatalog(configView.plugin_catalog)}>
                    添加所选插件
                  </Button>
                </div>

                {plugins.length === 0 ? (
                  <p className="muted">当前尚未声明任何插件，可从上方下拉框选择 notebook、clock、bed、qq 等插件一键插入默认模板。</p>
                ) : (
                  <div className="studio-stack">
                    {plugins.map((p, idx) => (
                      <article key={`${p.entry}-${idx}`} className="config-card">
                        <div className="object-header">
                          <h3>
                            {p.instance_id} <span className="identity">（entry = "{p.entry}"）</span>
                          </h3>
                          <div className="actions">
                            <Switch
                              label={`启用插件 ${p.instance_id}`}
                              checked={p.enabled}
                              onCheckedChange={(enabled) => {
                                const copy = [...plugins];
                                copy[idx] = { ...p, enabled };
                                setPlugins(copy);
                              }}
                            />
                            <Button
                              variant="ghost"
                              onClick={() => {
                                const nextPlugins = plugins.filter((_, i) => i !== idx);
                                const nextDrafts: Record<number, string> = {};
                                nextPlugins.forEach((item, newIdx) => {
                                  const origIdx = plugins.indexOf(item);
                                  nextDrafts[newIdx] = pluginJsonDrafts[origIdx] ?? JSON.stringify(item.config, null, 2);
                                });
                                setPlugins(nextPlugins);
                                setPluginJsonDrafts(nextDrafts);
                              }}
                            >
                              删除
                            </Button>
                          </div>
                        </div>
                        <div className="config-grid">
                          <Field label="实例名称（instance_id）">
                            {(control) => (
                              <input
                                {...control}
                                type="text"
                                value={p.instance_id}
                                onChange={(e) => {
                                  const copy = [...plugins];
                                  copy[idx] = { ...p, instance_id: e.target.value };
                                  setPlugins(copy);
                                }}
                              />
                            )}
                          </Field>
                          <Field label="插件入口（entry）">
                            {(control) => (
                              <input
                                {...control}
                                type="text"
                                value={p.entry}
                                onChange={(e) => {
                                  const copy = [...plugins];
                                  copy[idx] = { ...p, entry: e.target.value };
                                  setPlugins(copy);
                                }}
                              />
                            )}
                          </Field>
                        </div>
                        <Field label="插件参数（config JSON）">
                          {(control) => (
                            <textarea
                              {...control}
                              className="code-editor"
                              rows={4}
                              value={pluginJsonDrafts[idx] ?? "{}"}
                              onChange={(e) => setPluginJsonDrafts({ ...pluginJsonDrafts, [idx]: e.target.value })}
                            />
                          )}
                        </Field>
                      </article>
                    ))}
                  </div>
                )}
              </Section>
            </form>
          ) : (
            <Section
              id="raw-toml-editor"
              title={`TOML 源码直接编辑（${configView.config_name}）`}
              aside={
                <Button loading={saving} onClick={() => void handleRawSave()}>
                  校验并保存 TOML
                </Button>
              }
            >
              <p className="muted">
                明文 API Key 已在源码中自动掩码为 <code>********</code>；只要保持这串星号不改，保存时会自动填回磁盘上的真实密钥。你也可以直接改成新的密钥或 <code>{"${ENV_VAR}"}</code>。
              </p>
              <Field label="purrcept.toml 内容">
                {(control) => (
                  <textarea
                    {...control}
                    className="code-editor"
                    rows={22}
                    value={rawToml}
                    onChange={(e) => setRawToml(e.target.value)}
                  />
                )}
              </Field>
            </Section>
          )}
        </>
      ) : null}

      <Section id="system-health" title="核心运行健康">
        {!s ? (
          <EmptyNote icon="settings">正在连接本机 Engine 获取运行状态…</EmptyNote>
        ) : (
          <>
            <div className="flat-list">
              <div className="flat-row">
                <span>引擎状态</span>
                <StateMark {...mark(s.phase)} />
              </div>
              <div className="flat-row">
                <span>本地权威数据库</span>
                <StateMark state={s.authority_available ? "completed" : "failed"} label={s.authority_available ? "正常读写" : "不可用"} />
              </div>
              <div className="flat-row">
                <span>消息与刺激处理资格</span>
                <StateMark state={s.health.can_process ? "completed" : "warning"} label={s.health.can_process ? "正常接管" : "已暂停"} />
              </div>
              {failedComponents.map((comp) => (
                <div key={comp.component} className="flat-row">
                  <span>异常模块：{COMPONENT_LABELS[comp.component] ?? comp.component}</span>
                  <StateMark state="failed" label={comp.error_type ?? "故障"} />
                </div>
              ))}
            </div>
            <TechDetails
              items={[
                ["实例 ID", s.instance_id],
                ["待归还资源", s.pending_resources],
                ["当前积压条数", s.pending_inputs.reduce((sum, item) => sum + item.count, 0)],
                ["队列上限", s.pending_limits.count],
              ]}
            />
          </>
        )}
      </Section>
    </>
  );
}
