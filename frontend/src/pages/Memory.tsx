/**
 * 记忆手账工作台：浏览、搜索、新建与编辑 Notebook Markdown 文件（随笔、长期记忆与技能卡片）。
 */
import { useEffect, useState, type FormEvent, type ReactElement } from "react";
import { Button, Feedback, Field } from "@purrcept/cel";
import type { PanelClient } from "../client";
import type { NotebookFileContent, NotebookFileSummary, NotebooksOverview } from "../model/admin";
import type { Route } from "../model/routes";
import { EmptyState, ItemRow, PageHeading, relative } from "../ui/common";

type CategoryFilter = "all" | "journal" | "memory" | "skills" | "notes";

const CATEGORIES: readonly { key: CategoryFilter; label: string }[] = [
  { key: "all", label: "全部" },
  { key: "journal", label: "亲笔随笔" },
  { key: "memory", label: "长期记忆" },
  { key: "skills", label: "技能卡片" },
  { key: "notes", label: "普通笔记" },
];

function categoryBadge(cat: NotebookFileSummary["category"]): string {
  switch (cat) {
    case "journal":
      return "随笔";
    case "memory":
      return "记忆";
    case "skills":
      return "技能";
    case "notes":
      return "笔记";
  }
}

export function Memory({
  client,
  selected,
  go,
}: {
  client: PanelClient;
  selected: string | null;
  go: (route: Route) => void;
}): ReactElement {
  const [overview, setOverview] = useState<NotebooksOverview | null>(null);
  const [instanceId, setInstanceId] = useState<string>("");
  const [filter, setFilter] = useState<CategoryFilter>("all");
  const [query, setQuery] = useState("");
  const [activeFile, setActiveFile] = useState<NotebookFileContent | null>(null);
  const [draftContent, setDraftContent] = useState("");
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

  const currentInstance = overview?.instances.find((item) => item.instance_id === instanceId) ?? overview?.instances[0] ?? null;

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
      setNotice(`已保存 ${res.path}`);
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
    setNewPath(`journal/${today}.md`);
    setDraftContent(`# ${today} 随笔\n\n`);
    setDirty(true);
    setNotice(null);
  };

  const files = (currentInstance?.files ?? []).filter((f) => {
    if (filter !== "all" && f.category !== filter) return false;
    if (!query.trim()) return true;
    const q = query.trim().toLowerCase();
    return f.title.toLowerCase().includes(q) || f.path.toLowerCase().includes(q) || f.description.toLowerCase().includes(q);
  });

  const hasEditor = creating || activeFile !== null;
  const now = Date.now();

  return (
    <>
      <PageHeading
        title="记忆与技能手账"
        aside={
          <div className="actions">
            <Button variant="secondary" onClick={() => void loadList()} loading={loading}>
              刷新目录
            </Button>
            {overview?.available ? <Button onClick={startNewNote}>新建笔记</Button> : null}
          </div>
        }
      >
        直接查看与编辑她的 Markdown 随笔（journal/）、专题长期记忆（memory/）及按需挂载的技能卡（带 description 前言）。
      </PageHeading>

      {error ? (
        <Feedback state="failed" scope="region" urgency="passive" persistence="retained" title="操作提示">
          {error}
        </Feedback>
      ) : null}
      {notice ? (
        <Feedback state="completed" scope="region" urgency="passive" persistence="transient" onDismiss={() => setNotice(null)} title={notice} />
      ) : null}

      {overview && !overview.available ? (
        <EmptyState
          icon="review"
          title="尚未绑定笔记本目录"
          action={
            <Button onClick={() => go({ page: "system", id: null })}>
              前往设置启用 notebook 插件
            </Button>
          }
        >
          当前以无配置文件的嵌入模式运行。通过 purrcept.toml 启动后即可在此管理 notes/ 目录下的全部记忆文件。
        </EmptyState>
      ) : (
        <>
          <div className="timeline-tools">
            <fieldset className="toolbar">
              <legend className="visually-hidden">按记忆类别筛选</legend>
              {CATEGORIES.map((cat) => (
                <label key={cat.key} className="filter" data-selected={filter === cat.key}>
                  <input
                    type="radio"
                    name="memory-category"
                    checked={filter === cat.key}
                    onChange={() => setFilter(cat.key)}
                  />
                  {cat.label}
                </label>
              ))}
            </fieldset>
            <Field label="搜索笔记">
              {(control) => (
                <input
                  {...control}
                  type="search"
                  placeholder="标题、路径或摘要关键词"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                />
              )}
            </Field>
          </div>

          <div className="review-workspace" data-selected={hasEditor}>
            <div className="review-list">
              <div className="object-header">
                <h2>
                  {currentInstance ? `目录：${currentInstance.root_dir}/` : "笔记列表"}
                </h2>
                <span className="section-count">{files.length} 篇</span>
              </div>
              {files.length === 0 ? (
                <p className="muted">当前筛选下还没有笔记，点击右上角「新建笔记」即可创建第一篇 Markdown 手账。</p>
              ) : (
                <div className="item-list">
                  {files.map((file) => (
                    <ItemRow
                      key={file.path}
                      icon="review"
                      title={file.title}
                      subtitle={`${file.path} · ${relative(file.updated_at, now)}${file.description ? ` · ${file.description}` : ""}`}
                      trailing={<span className="section-count">{categoryBadge(file.category)}</span>}
                      current={!creating && activeFile?.path === file.path}
                      onClick={() => go({ page: "memory", id: file.path })}
                    />
                  ))}
                </div>
              )}
            </div>

            {hasEditor ? (
              <form className="review-material note-editor" onSubmit={(e) => void handleSave(e)}>
                <div className="object-header">
                  <h2>{creating ? "新建 Markdown 笔记" : activeFile?.path}</h2>
                  <div className="actions">
                    <Button
                      variant="ghost"
                      onClick={() => {
                        setCreating(false);
                        setActiveFile(null);
                        go({ page: "memory", id: null });
                      }}
                    >
                      返回列表
                    </Button>
                    <Button
                      type="submit"
                      loading={saving}
                      disabledReason={!creating && !dirty ? "内容尚未修改。" : undefined}
                    >
                      保存笔记
                    </Button>
                  </div>
                </div>

                {creating ? (
                  <Field
                    label="相对路径（须以 .md 结尾）"
                    description="例如 journal/2026-11-15.md、memory/user-preferences.md 或 skills/how-to-search.md"
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

                <Field label="Markdown 正文（技能卡可在顶部使用 --- YAML frontmatter 声明 name 与 description）">
                  {(control) => (
                    <textarea
                      {...control}
                      className="code-editor"
                      rows={18}
                      value={draftContent}
                      onChange={(e) => {
                        setDraftContent(e.target.value);
                        setDirty(true);
                      }}
                    />
                  )}
                </Field>
              </form>
            ) : null}
          </div>
        </>
      )}
    </>
  );
}
