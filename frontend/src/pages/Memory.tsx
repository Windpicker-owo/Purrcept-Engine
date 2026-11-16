/**
 * 暖调记忆手账工作台（Cozy Notebook 场域）：
 * 动态读取配置的 Bot 角色名（未配置时使用通用标题），搭配滑块分类标签与书页切换微动效。
 */
import { useEffect, useMemo, useState, type FormEvent, type ReactElement } from "react";
import { AnimatePresence, motion } from "motion/react";
import { Button, Feedback, Field, transitions } from "@purrcept/cel";
import type { PanelClient } from "../client";
import type { NotebookFileContent, NotebookFileSummary, NotebooksOverview } from "../model/admin";
import type { Route } from "../model/routes";
import { EmptyState, FilterPill, PageHeading, relative, Section } from "../ui/common";

type CategoryFilter = "all" | "journal" | "memory" | "skills" | "notes";
type EditorMode = "read" | "edit";

const CATEGORIES: readonly { key: CategoryFilter; label: string }[] = [
  { key: "all", label: "全部手账" },
  { key: "journal", label: "亲笔随笔" },
  { key: "memory", label: "长期记忆" },
  { key: "skills", label: "技能卡片" },
  { key: "notes", label: "日常便签" },
];

function categoryBadge(cat: NotebookFileSummary["category"]): string {
  switch (cat) {
    case "journal":
      return "亲笔随笔";
    case "memory":
      return "长期记忆";
    case "skills":
      return "技能卡";
    case "notes":
      return "便签";
  }
}

function inferCategoryFromPath(path: string): NotebookFileSummary["category"] {
  if (path.startsWith("journal/")) return "journal";
  if (path.startsWith("memory/")) return "memory";
  if (path.startsWith("skills/")) return "skills";
  return "notes";
}

interface ParsedMarkdown {
  readonly meta: readonly [string, string][];
  readonly blocks: readonly (
    | { readonly kind: "h1" | "h2" | "h3" | "p" | "quote"; readonly text: string }
    | { readonly kind: "list"; readonly items: readonly string[] }
    | { readonly kind: "code"; readonly text: string }
  )[];
}

function parseNotebookMarkdown(source: string): ParsedMarkdown {
  let body = source.replace(/\r\n/g, "\n");
  const meta: [string, string][] = [];

  if (body.startsWith("---\n")) {
    const end = body.indexOf("\n---\n", 4);
    if (end !== -1) {
      const front = body.slice(4, end).split("\n");
      for (const rawLine of front) {
        const sep = rawLine.indexOf(":");
        if (sep > 0) {
          const k = rawLine.slice(0, sep).trim();
          const v = rawLine.slice(sep + 1).trim().replace(/^["']|["']$/g, "");
          if (k && v) meta.push([k, v]);
        }
      }
      body = body.slice(end + 5);
    }
  }

  const lines = body.split("\n");
  const blocks: (
    | { readonly kind: "h1" | "h2" | "h3" | "p" | "quote"; readonly text: string }
    | { readonly kind: "list"; readonly items: readonly string[] }
    | { readonly kind: "code"; readonly text: string }
  )[] = [];

  let i = 0;
  while (i < lines.length) {
    const line = lines[i] ?? "";
    const trimmed = line.trim();
    if (!trimmed) {
      i++;
      continue;
    }
    if (trimmed.startsWith("```")) {
      const codeLines: string[] = [];
      i++;
      while (i < lines.length && !(lines[i] ?? "").trim().startsWith("```")) {
        codeLines.push(lines[i] ?? "");
        i++;
      }
      if (i < lines.length) i++;
      blocks.push({ kind: "code", text: codeLines.join("\n") });
      continue;
    }
    if (trimmed.startsWith("# ")) {
      blocks.push({ kind: "h1", text: trimmed.slice(2).trim() });
      i++;
      continue;
    }
    if (trimmed.startsWith("## ")) {
      blocks.push({ kind: "h2", text: trimmed.slice(3).trim() });
      i++;
      continue;
    }
    if (trimmed.startsWith("### ")) {
      blocks.push({ kind: "h3", text: trimmed.slice(4).trim() });
      i++;
      continue;
    }
    if (trimmed.startsWith(">")) {
      const quotes: string[] = [];
      while (i < lines.length && (lines[i] ?? "").trim().startsWith(">")) {
        quotes.push((lines[i] ?? "").trim().replace(/^>\s?/, ""));
        i++;
      }
      blocks.push({ kind: "quote", text: quotes.join("\n") });
      continue;
    }
    if (/^[-*+]\s+/.test(trimmed)) {
      const items: string[] = [];
      while (i < lines.length && /^[-*+]\s+/.test((lines[i] ?? "").trim())) {
        items.push((lines[i] ?? "").trim().replace(/^[-*+]\s+/, ""));
        i++;
      }
      blocks.push({ kind: "list", items });
      continue;
    }
    const para: string[] = [trimmed];
    i++;
    while (
      i < lines.length &&
      (lines[i] ?? "").trim() !== "" &&
      !/^(#{1,3}\s|```|>|[-*+]\s+)/.test((lines[i] ?? "").trim())
    ) {
      para.push((lines[i] ?? "").trim());
      i++;
    }
    blocks.push({ kind: "p", text: para.join(" ") });
  }

  return { meta, blocks };
}

export function Memory({
  client,
  selected,
  botName,
  go,
}: {
  client: PanelClient;
  selected: string | null;
  botName: string;
  go: (route: Route) => void;
}): ReactElement {
  const [overview, setOverview] = useState<NotebooksOverview | null>(null);
  const [instanceId, setInstanceId] = useState<string>("");
  const [filter, setFilter] = useState<CategoryFilter>("all");
  const [query, setQuery] = useState("");
  const [activeFile, setActiveFile] = useState<NotebookFileContent | null>(null);
  const [draftContent, setDraftContent] = useState("");
  const [mode, setMode] = useState<EditorMode>("read");
  const [dirty, setDirty] = useState(false);
  const [creating, setCreating] = useState(false);
  const [newPath, setNewPath] = useState("journal/today.md");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const loadList = async (): Promise<void> => {
    setLoading(true);
    try {
      const res = await client.listNotebooks();
      setOverview(res);
      if (res.instances.length > 0 && !res.instances.some((item) => item.instance_id === instanceId)) {
        setInstanceId(res.instances[0]!.instance_id);
      }
      setError(null);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "读取笔记本目录失败。");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void loadList();
  }, [client]);

  const currentInstance =
    overview?.instances.find((item) => item.instance_id === instanceId) ?? overview?.instances[0] ?? null;

  useEffect(() => {
    if (!selected || !currentInstance) return;
    let active = true;
    const openSelected = async (): Promise<void> => {
      try {
        const doc = await client.readNote(currentInstance.instance_id, selected);
        if (!active) return;
        setActiveFile(doc);
        setDraftContent(doc.content);
        setDirty(false);
        setCreating(false);
        setMode("read");
        setError(null);
      } catch (err: unknown) {
        if (active) setError(err instanceof Error ? err.message : "读取笔记正文失败。");
      }
    };
    void openSelected();
    return () => {
      active = false;
    };
  }, [selected, currentInstance?.instance_id, client]);

  const handleSave = async (event?: FormEvent): Promise<void> => {
    event?.preventDefault();
    if (!currentInstance || saving) return;
    const targetPath = creating ? newPath.trim() : activeFile?.path ?? "";
    if (!targetPath) return;
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const res = await client.saveNote(
        currentInstance.instance_id,
        targetPath,
        draftContent,
        creating ? "missing" : activeFile?.revision ?? null,
      );
      setActiveFile({
        instance: currentInstance.instance_id,
        path: res.path,
        content: draftContent,
        revision: res.revision,
        updated_at: res.updated_at,
      });
      setDirty(false);
      setCreating(false);
      setMode("read");
      setNotice(`已妥善夹入手账：${res.path}`);
      await loadList();
      go({ page: "memory", id: res.path });
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "保存笔记失败。");
    } finally {
      setSaving(false);
    }
  };

  const startNewNote = (): void => {
    const today = new Date().toISOString().slice(0, 10);
    setCreating(true);
    setMode("edit");
    setNewPath(`journal/${today}.md`);
    setDraftContent(`# ${today} 日常随笔\n\n记录今天值得留下的片段——\n`);
    setDirty(true);
    setNotice(null);
  };

  const files = (currentInstance?.files ?? []).filter((f) => {
    if (filter !== "all" && f.category !== filter) return false;
    if (!query.trim()) return true;
    const q = query.trim().toLowerCase();
    return (
      f.title.toLowerCase().includes(q) ||
      f.path.toLowerCase().includes(q) ||
      f.description.toLowerCase().includes(q)
    );
  });

  const hasEditor = creating || activeFile !== null;
  const parsed = useMemo(() => parseNotebookMarkdown(draftContent), [draftContent]);
  const activePath = creating ? newPath : activeFile?.path ?? "";
  const activeCategory = inferCategoryFromPath(activePath);
  const resolvedBot = botName.trim();
  const now = Date.now();

  return (
    <>
      <PageHeading
        eyebrow="COZY NOTEBOOK // DIARY & MEMORY"
        tone="notebook"
        title={resolvedBot ? `${resolvedBot}的记忆与手账本` : "记忆与技能手账本"}
        aside={
          <div className="actions">
            <Button variant="secondary" onClick={() => void loadList()} loading={loading}>
              刷新手账
            </Button>
            {overview?.available ? <Button onClick={startNewNote}>写新随笔</Button> : null}
          </div>
        }
      >
        翻阅在方格纸上亲笔写下的日常随笔（journal/）、与你有关的长期记忆（memory/），以及按需挂载的技能便签（skills/）。
      </PageHeading>

      {error ? (
        <Feedback state="failed" scope="region" urgency="passive" persistence="retained" title="手账操作提示">
          {error}
        </Feedback>
      ) : null}
      {notice ? (
        <Feedback
          state="completed"
          scope="region"
          urgency="passive"
          persistence="transient"
          onDismiss={() => setNotice(null)}
          title={notice}
        />
      ) : null}

      {overview && !overview.available ? (
        <EmptyState
          icon="review"
          title="尚未绑定手账本目录"
          action={
            <Button onClick={() => go({ page: "system", id: null })}>
              前往设置启用 notebook 插件
            </Button>
          }
        >
          当前以无配置文件的嵌入模式运行。通过 purrcept.toml 启动后，即可在此翻阅并编辑 notes/ 目录下的全部记忆手账。
        </EmptyState>
      ) : (
        <>
          <div className="timeline-tools notebook-tools">
            <fieldset className="toolbar notebook-tabs">
              <legend className="visually-hidden">按手账分类筛选</legend>
              {CATEGORIES.map((cat) => (
                <FilterPill
                  key={cat.key}
                  group="memory-category"
                  value={cat.key}
                  label={cat.label}
                  category={cat.key}
                  checked={filter === cat.key}
                  onChange={() => setFilter(cat.key)}
                />
              ))}
            </fieldset>
            <Field label="搜索手账">
              {(control) => (
                <input
                  {...control}
                  type="search"
                  placeholder="标题、路径或手账摘要关键词"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                />
              )}
            </Field>
          </div>

          <div className="review-workspace notebook-workspace" data-selected={hasEditor}>
            <div className="notebook-index-wrap">
              <Section
                id="notebook-index"
                surface="notebook"
                eyebrow="INDEX // 手账目录"
                title={currentInstance ? `手账夹：${currentInstance.root_dir}/` : "手账目录"}
                aside={<span className="notebook-count">{files.length} 篇</span>}
              >
                {files.length === 0 ? (
                  <div className="notebook-empty">
                    <p className="notebook-warm-note">
                      这页索引还是空白的。点击右上角「写新随笔」，就能在暖调方格纸上留下第一篇记录。
                    </p>
                  </div>
                ) : (
                  <div className="notebook-list">
                    {files.map((file) => {
                      const isCurrent = !creating && activeFile?.path === file.path;
                      return (
                        <button
                          key={file.path}
                          type="button"
                          className="notebook-entry pc-focusable"
                          aria-current={isCurrent ? "true" : undefined}
                          onClick={() => go({ page: "memory", id: file.path })}
                        >
                          <div className="notebook-entry-top">
                            <span className="note-sticker" data-category={file.category}>
                              {categoryBadge(file.category)}
                            </span>
                            <span className="notebook-entry-time">{relative(file.updated_at, now)}</span>
                          </div>
                          <span className="notebook-entry-title">{file.title}</span>
                          <span className="notebook-entry-path">{file.path}</span>
                          {file.description ? (
                            <span className="notebook-entry-desc">{file.description}</span>
                          ) : null}
                        </button>
                      );
                    })}
                  </div>
                )}
              </Section>
            </div>

            {hasEditor ? (
              <form className="review-material notebook-sheet" onSubmit={(e) => void handleSave(e)}>
                <div className="notebook-sheet-header">
                  <div className="notebook-sheet-titles">
                    <div className="notebook-sheet-badges">
                      <span className="note-sticker" data-category={activeCategory}>
                        {categoryBadge(activeCategory)}
                      </span>
                      <span className="notebook-meta-stamp">
                        {creating
                          ? "新页草稿 · 尚未落盘"
                          : activeFile
                            ? `最近落笔：${relative(activeFile.updated_at, now)}`
                            : ""}
                      </span>
                    </div>
                    <h2>{creating ? "执笔写新随笔" : activeFile?.path}</h2>
                  </div>

                  <div className="actions">
                    {!creating ? (
                      <Button
                        variant="secondary"
                        onClick={() => setMode((m) => (m === "read" ? "edit" : "read"))}
                      >
                        {mode === "read" ? "编辑手账源码" : "预览纸面排版"}
                      </Button>
                    ) : null}
                    <Button
                      variant="ghost"
                      onClick={() => {
                        setCreating(false);
                        setActiveFile(null);
                        go({ page: "memory", id: null });
                      }}
                    >
                      收起手账页
                    </Button>
                    {creating || mode === "edit" || dirty ? (
                      <Button
                        type="submit"
                        loading={saving}
                        disabledReason={!creating && !dirty ? "内容尚未修改。" : undefined}
                      >
                        保存手账
                      </Button>
                    ) : null}
                  </div>
                </div>

                {creating ? (
                  <Field
                    label="手账归档路径（须以 .md 结尾）"
                    description="例如 journal/2026-11-16.md（亲笔随笔）、memory/preferences.md（长期记忆）或 skills/search-tips.md（技能卡）"
                  >
                    {(control) => (
                      <input
                        {...control}
                        type="text"
                        value={newPath}
                        onChange={(e) => setNewPath(e.target.value)}
                      />
                    )}
                  </Field>
                ) : null}

                <AnimatePresence mode="wait" initial={false}>
                  {!creating && mode === "read" ? (
                    <motion.div
                      key={`read-${activeFile?.path ?? "draft"}`}
                      className="notebook-reading-margin"
                      initial={{ opacity: 0, y: 4 }}
                      animate={{ opacity: 1, y: 0 }}
                      exit={{ opacity: 0, y: -3 }}
                      transition={transitions.snap}
                    >
                      {parsed.meta.length > 0 ? (
                        <dl className="notebook-frontmatter">
                          {parsed.meta.map(([k, v]) => (
                            <div key={k} className="notebook-frontmatter-chip">
                              <dt>{k}</dt>
                              <dd>{v}</dd>
                            </div>
                          ))}
                        </dl>
                      ) : null}
                      {parsed.blocks.length === 0 ? (
                        <p className="notebook-warm-note">这页手账目前是空白的，点击上方「编辑手账源码」即可添上内容。</p>
                      ) : (
                        <article className="notebook-prose">
                          {parsed.blocks.map((block, idx) => {
                            if (block.kind === "h1") return <h3 key={idx} className="notebook-h1">{block.text}</h3>;
                            if (block.kind === "h2") return <h4 key={idx} className="notebook-h2">{block.text}</h4>;
                            if (block.kind === "h3") return <h5 key={idx} className="notebook-h3">{block.text}</h5>;
                            if (block.kind === "quote") {
                              return <blockquote key={idx} className="notebook-quote">{block.text}</blockquote>;
                            }
                            if (block.kind === "list") {
                              return (
                                <ul key={idx} className="notebook-ul">
                                  {block.items.map((item, j) => (
                                    <li key={j}>{item}</li>
                                  ))}
                                </ul>
                              );
                            }
                            if (block.kind === "code") {
                              return <pre key={idx} className="notebook-code">{block.text}</pre>;
                            }
                            return <p key={idx}>{block.text}</p>;
                          })}
                        </article>
                      )}
                    </motion.div>
                  ) : (
                    <motion.div
                      key="edit-mode"
                      className="notebook-reading-margin"
                      initial={{ opacity: 0, y: 4 }}
                      animate={{ opacity: 1, y: 0 }}
                      exit={{ opacity: 0, y: -3 }}
                      transition={transitions.snap}
                    >
                      <Field label="Markdown 手账正文（技能卡可在顶部用 --- YAML 声明 name 与 description）">
                        {(control) => (
                          <textarea
                            {...control}
                            className="code-editor notebook-textarea"
                            rows={18}
                            value={draftContent}
                            onChange={(e) => {
                              setDraftContent(e.target.value);
                              setDirty(true);
                            }}
                          />
                        )}
                      </Field>
                    </motion.div>
                  )}
                </AnimatePresence>
              </form>
            ) : null}
          </div>
        </>
      )}
    </>
  );
}
