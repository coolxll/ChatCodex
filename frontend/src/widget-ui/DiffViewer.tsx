import { useEffect, useMemo, useState } from "react";
import {
  Check,
  ChevronLeft,
  ChevronRight,
  Copy,
  Focus,
  MessageSquareText,
} from "lucide-react";
import type { DiffLine, FileDiff } from "../lib/diff";
import { EmptyState } from "./EmptyState";

const INITIAL_LINE_LIMIT = 500;

export function DiffViewer({
  files,
  initialPath,
  onTarget,
  onFocus,
}: {
  files: FileDiff[];
  initialPath?: string;
  onTarget?(file: FileDiff): void;
  onFocus?(file: FileDiff): void;
}) {
  const [selectedPath, setSelectedPath] = useState(initialPath ?? files[0]?.path ?? "");
  const [lineLimit, setLineLimit] = useState(INITIAL_LINE_LIMIT);
  const [copied, setCopied] = useState(false);
  const selectedIndex = Math.max(0, files.findIndex((file) => file.path === selectedPath));
  const file = files[selectedIndex];
  useEffect(() => {
    if (!files.some((item) => item.path === selectedPath)) {
      setSelectedPath(files[0]?.path ?? "");
    }
  }, [files, selectedPath]);
  useEffect(() => {
    if (initialPath && files.some((item) => item.path === initialPath)) {
      setSelectedPath(initialPath);
    }
  }, [files, initialPath]);
  useEffect(() => setLineLimit(INITIAL_LINE_LIMIT), [selectedPath]);
  const visibleLines = useMemo(
    () => file?.lines.slice(0, lineLimit) ?? [],
    [file, lineLimit],
  );

  if (!files.length) {
    return <EmptyState title="没有可显示的改动" description="工具没有返回文件差异。" />;
  }

  const choose = (path: string) => {
    setSelectedPath(path);
    setCopied(false);
  };
  const copyPath = async () => {
    if (!file) return;
    await navigator.clipboard?.writeText(file.path);
    setCopied(true);
  };

  return (
    <div className="diff-viewer">
      <aside className="diff-files" aria-label="改动文件">
        {files.map((item) => (
          <button
            type="button"
            className="diff-file"
            data-active={item.path === file?.path}
            key={item.path}
            onClick={() => choose(item.path)}
          >
            <KindBadge kind={item.kind} />
            <span className="diff-file-path">{item.path}</span>
            <span className="diff-file-stats">
              {item.adds > 0 && <span className="diff-add">+{item.adds}</span>}
              {item.dels > 0 && <span className="diff-del">−{item.dels}</span>}
            </span>
          </button>
        ))}
      </aside>
      <section className="diff-document">
        {file && (
          <>
            <header className="diff-toolbar">
              <select
                className="diff-mobile-select"
                value={file.path}
                aria-label="选择改动文件"
                onChange={(event) => choose(event.target.value)}
              >
                {files.map((item) => <option value={item.path} key={item.path}>{item.path}</option>)}
              </select>
              <span className="diff-current-path" title={file.path}>{file.path}</span>
              <span className="diff-file-stats">
                <span className="diff-add">+{file.adds}</span>
                <span className="diff-del">−{file.dels}</span>
              </span>
              <button type="button" className="widget-icon-button" onClick={copyPath}>
                {copied ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
                {copied ? "已复制" : "复制路径"}
              </button>
              {onTarget && (
                <button type="button" className="widget-icon-button" onClick={() => onTarget(file)}>
                  <MessageSquareText aria-hidden="true" />在 ChatGPT 中提问
                </button>
              )}
              {onFocus && (
                <button type="button" className="widget-icon-button" onClick={() => onFocus(file)}>
                  <Focus aria-hidden="true" />聚焦查看
                </button>
              )}
            </header>
            <div className="diff-code" role="region" aria-label={`${file.path} 差异`}>
              {visibleLines.length ? visibleLines.map((line, index) => (
                <DiffRow line={line} key={`${selectedPath}-${index}`} />
              )) : (
                <EmptyState title="只有文件状态，没有逐行差异" />
              )}
              {file.lines.length > lineLimit && (
                <button
                  type="button"
                  className="diff-load-more"
                  onClick={() => setLineLimit((current) => current + INITIAL_LINE_LIMIT)}
                >
                  再显示 {Math.min(INITIAL_LINE_LIMIT, file.lines.length - lineLimit)} 行
                </button>
              )}
            </div>
            <footer className="diff-navigation">
              <button
                type="button"
                className="widget-icon-button"
                disabled={selectedIndex === 0}
                onClick={() => choose(files[selectedIndex - 1].path)}
              >
                <ChevronLeft aria-hidden="true" />上一文件
              </button>
              <span>{selectedIndex + 1} / {files.length}</span>
              <button
                type="button"
                className="widget-icon-button"
                disabled={selectedIndex === files.length - 1}
                onClick={() => choose(files[selectedIndex + 1].path)}
              >
                下一文件<ChevronRight aria-hidden="true" />
              </button>
            </footer>
          </>
        )}
      </section>
    </div>
  );
}

function DiffRow({ line }: { line: DiffLine }) {
  return (
    <div className="diff-line" data-kind={line.type}>
      <span className="diff-line-number">{line.oldNo ?? ""}</span>
      <span className="diff-line-number">{line.newNo ?? ""}</span>
      <span className="diff-line-sign" aria-hidden="true">
        {line.type === "add" ? "+" : line.type === "del" ? "−" : " "}
      </span>
      <span className="diff-line-text">{line.text}</span>
    </div>
  );
}

function KindBadge({ kind }: { kind: FileDiff["kind"] }) {
  const labels = { add: "A", delete: "D", update: "M", rename: "R" };
  return <span className="diff-kind" data-kind={kind}>{labels[kind]}</span>;
}
