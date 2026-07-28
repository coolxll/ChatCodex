import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Check,
  ChevronRight,
  CornerUpLeft,
  Folder,
  FolderOpen,
  History,
  Loader2,
  RefreshCw,
} from "lucide-react";
import * as OA from "./openai";
import { EmptyState, Notice } from "../widget-ui";

interface DirectoryEntry {
  name: string;
  path: string;
}

interface DirectoryResult {
  path: string;
  parent?: string | null;
  entries: DirectoryEntry[];
  error?: string;
}

interface BranchState {
  status: "loading" | "ready" | "error";
  entries: DirectoryEntry[];
  error?: string;
}

interface VisibleEntry {
  entry: DirectoryEntry;
  level: number;
  parentPath?: string;
}

export interface DirBrowserProps {
  initialPath?: string;
  onSelect?(path: string): void;
  selectedPath?: string;
  recentPaths?: string[];
}

async function browse(path: string): Promise<DirectoryResult> {
  const result = await OA.callTool<DirectoryResult>("browse_dir", { path });
  if (result.error) throw new Error(result.error);
  return {
    path: result.path || path,
    parent: result.parent,
    entries: Array.isArray(result.entries) ? result.entries : [],
  };
}

export function DirBrowser({
  initialPath = "",
  onSelect,
  selectedPath,
  recentPaths = [],
}: DirBrowserProps) {
  const [currentPath, setCurrentPath] = useState(initialPath);
  const [result, setResult] = useState<DirectoryResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [activePath, setActivePath] = useState("");
  const [expandedPaths, setExpandedPaths] = useState<Set<string>>(() => new Set());
  const [branches, setBranches] = useState<Record<string, BranchState>>({});
  const rowRefs = useRef<Map<string, HTMLDivElement>>(new Map());

  const loadRoot = useCallback(async (path: string) => {
    setLoading(true);
    setError("");
    setCurrentPath(path);
    setResult(null);
    setExpandedPaths(new Set());
    setBranches({});
    try {
      const next = await browse(path);
      setResult(next);
      setCurrentPath(next.path);
      setActivePath(next.entries[0]?.path ?? "");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadRoot(initialPath);
  }, [initialPath, loadRoot]);

  const entries = result?.entries ?? [];
  const crumbs = useMemo(() => pathCrumbs(currentPath), [currentPath]);
  const uniqueRecent = useMemo(
    () => Array.from(new Set(recentPaths.filter(Boolean))).slice(0, 5),
    [recentPaths],
  );
  const visibleEntries = useMemo(() => {
    const visible: VisibleEntry[] = [];
    const visited = new Set<string>();
    const append = (
      items: DirectoryEntry[],
      level: number,
      parentPath?: string,
    ) => {
      for (const entry of items) {
        visible.push({ entry, level, parentPath });
        if (!expandedPaths.has(entry.path) || visited.has(entry.path)) continue;
        visited.add(entry.path);
        const branch = branches[entry.path];
        if (branch?.status === "ready") append(branch.entries, level + 1, entry.path);
      }
    };
    append(entries, 1);
    return visible;
  }, [branches, entries, expandedPaths]);

  function focusPath(path: string) {
    if (!path) return;
    setActivePath(path);
    window.requestAnimationFrame(() => rowRefs.current.get(path)?.focus());
  }

  async function loadBranch(path: string) {
    setBranches((current) => ({
      ...current,
      [path]: { status: "loading", entries: current[path]?.entries ?? [] },
    }));
    try {
      const next = await browse(path);
      setBranches((current) => ({
        ...current,
        [path]: { status: "ready", entries: next.entries },
      }));
    } catch (cause) {
      setBranches((current) => ({
        ...current,
        [path]: {
          status: "error",
          entries: [],
          error: cause instanceof Error ? cause.message : String(cause),
        },
      }));
    }
  }

  function expand(path: string) {
    setExpandedPaths((current) => new Set(current).add(path));
    if (branches[path]?.status !== "ready" && branches[path]?.status !== "loading") {
      void loadBranch(path);
    }
  }

  function collapse(path: string) {
    setExpandedPaths((current) => {
      const next = new Set(current);
      next.delete(path);
      return next;
    });
  }

  function toggle(path: string) {
    if (expandedPaths.has(path)) collapse(path);
    else expand(path);
  }

  function handleTreeKey(event: React.KeyboardEvent, item: VisibleEntry) {
    const { entry, parentPath } = item;
    const index = visibleEntries.findIndex((visible) => visible.entry.path === entry.path);
    if (event.key === "ArrowDown") {
      event.preventDefault();
      focusPath(visibleEntries[Math.min(visibleEntries.length - 1, index + 1)]?.entry.path);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      focusPath(visibleEntries[Math.max(0, index - 1)]?.entry.path);
    } else if (event.key === "Home") {
      event.preventDefault();
      focusPath(visibleEntries[0]?.entry.path);
    } else if (event.key === "End") {
      event.preventDefault();
      focusPath(visibleEntries[visibleEntries.length - 1]?.entry.path);
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      if (!expandedPaths.has(entry.path)) expand(entry.path);
      else {
        const firstChild = branches[entry.path]?.entries[0];
        if (firstChild) focusPath(firstChild.path);
      }
    } else if (event.key === "ArrowLeft") {
      event.preventDefault();
      if (expandedPaths.has(entry.path)) collapse(entry.path);
      else if (parentPath) focusPath(parentPath);
    } else if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      onSelect?.(entry.path);
    }
  }

  function renderEntries(items: DirectoryEntry[], level: number, parentPath?: string): React.ReactNode {
    return items.map((entry) => {
      const selected = selectedPath === entry.path;
      const expanded = expandedPaths.has(entry.path);
      const branch = branches[entry.path];
      const item = { entry, level, parentPath };
      return (
        <React.Fragment key={entry.path}>
          <div
            ref={(node) => {
              if (node) rowRefs.current.set(entry.path, node);
              else rowRefs.current.delete(entry.path);
            }}
            role="treeitem"
            aria-level={level}
            aria-selected={selected}
            aria-expanded={expanded}
            tabIndex={entry.path === activePath ? 0 : -1}
            className="directory-row"
            data-selected={selected}
            data-expanded={expanded}
            style={{ paddingInlineStart: `${0.625 + (level - 1) * 1.125}rem` }}
            onClick={() => onSelect?.(entry.path)}
            onFocus={() => setActivePath(entry.path)}
            onKeyDown={(event) => handleTreeKey(event, item)}
          >
            <Folder aria-hidden="true" />
            <span>{entry.name}</span>
            {selected && <Check aria-hidden="true" className="directory-check" />}
            <button
              type="button"
              className="directory-open"
              aria-label={`${expanded ? "折叠" : "展开"} ${entry.name}`}
              aria-expanded={expanded}
              onClick={(event) => {
                event.stopPropagation();
                toggle(entry.path);
              }}
            >
              <ChevronRight aria-hidden="true" />
            </button>
          </div>
          {expanded && (
            <div role="group" className="directory-branch">
              {branch?.status === "loading" && (
                <div className="directory-branch-state" style={{ paddingInlineStart: `${1.75 + level * 1.125}rem` }}>
                  <Loader2 aria-hidden="true" className="animate-spin" />正在读取
                </div>
              )}
              {branch?.status === "error" && (
                <div className="directory-branch-state directory-branch-error" style={{ paddingInlineStart: `${1.75 + level * 1.125}rem` }}>
                  <span>{branch.error}</span>
                  <button type="button" className="widget-link-button" onClick={() => void loadBranch(entry.path)}>
                    重试
                  </button>
                </div>
              )}
              {branch?.status === "ready" && branch.entries.length === 0 && (
                <div className="directory-branch-state" style={{ paddingInlineStart: `${1.75 + level * 1.125}rem` }}>
                  没有子目录
                </div>
              )}
              {branch?.status === "ready" && renderEntries(branch.entries, level + 1, entry.path)}
            </div>
          )}
        </React.Fragment>
      );
    });
  }

  return (
    <div className="directory-browser">
      {uniqueRecent.length > 0 && (
        <div className="directory-recents" aria-label="最近目录">
          <span className="directory-recents-label"><History aria-hidden="true" />最近</span>
          {uniqueRecent.map((path) => (
           <button type="button" key={path} onClick={() => {
             onSelect?.(path);
              void loadRoot(path);
            }}>{compactPath(path)}</button>
          ))}
        </div>
      )}

      <div className="directory-toolbar">
        <button
          type="button"
          className="widget-icon-button"
          disabled={!result?.parent}
          onClick={() => result?.parent && void loadRoot(result.parent)}
        >
          <CornerUpLeft aria-hidden="true" />上一级
        </button>
        <nav className="directory-breadcrumbs" aria-label="当前目录">
          {crumbs.map((crumb, index) => (
            <React.Fragment key={crumb.path}>
              {index > 0 && <ChevronRight aria-hidden="true" />}
              <button type="button" title={crumb.path} onClick={() => void loadRoot(crumb.path)}>
                {crumb.label}
              </button>
            </React.Fragment>
          ))}
        </nav>
        <button
          type="button"
          className="widget-icon-button"
          aria-label="重新加载目录"
          onClick={() => void loadRoot(currentPath)}
        >
          <RefreshCw aria-hidden="true" />
        </button>
      </div>

      <div className="directory-selection" title={selectedPath || "尚未选择"}>
        <FolderOpen aria-hidden="true" />
        <span>{selectedPath || "单击一个目录以选择"}</span>
      </div>

      {loading ? (
        <div className="widget-skeleton"><Loader2 aria-hidden="true" className="animate-spin" />正在读取目录</div>
      ) : error ? (
        <Notice tone="danger" role="alert">
          <div>{error}</div>
          <button type="button" className="widget-link-button" onClick={() => void loadRoot(currentPath)}>
            重试
          </button>
        </Notice>
      ) : entries.length === 0 ? (
        <EmptyState title="这个目录中没有子目录" description="可以选择当前目录，或返回上一级。" action={
          <button type="button" className="widget-button widget-button-secondary" onClick={() => onSelect?.(currentPath)}>
            选择当前目录
          </button>
        } />
      ) : (
        <div className="directory-tree" role="tree" aria-label={currentPath}>
          {renderEntries(entries, 1)}
        </div>
      )}
    </div>
  );
}

function pathCrumbs(path: string): Array<{ label: string; path: string }> {
  if (!path) return [{ label: "主目录", path: "" }];
  const windows = /^[A-Za-z]:[\\/]/.test(path);
  if (windows) {
    const normalized = path.replace(/\//g, "\\");
    const [drive, ...parts] = normalized.split("\\").filter(Boolean);
    const crumbs = [{ label: drive, path: `${drive}\\` }];
    let current = `${drive}\\`;
    for (const part of parts) {
      current = `${current}${current.endsWith("\\") ? "" : "\\"}${part}`;
      crumbs.push({ label: part, path: current });
    }
    return crumbs;
  }
  const parts = path.split("/").filter(Boolean);
  const crumbs = [{ label: "/", path: "/" }];
  let current = "";
  for (const part of parts) {
    current += `/${part}`;
    crumbs.push({ label: part, path: current });
  }
  return crumbs;
}

function compactPath(path: string): string {
  const normalized = path.replace(/[\\/]+$/, "");
  const parts = normalized.split(/[\\/]/).filter(Boolean);
  return parts[parts.length - 1] || path;
}
