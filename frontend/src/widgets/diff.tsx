import { useEffect, useMemo, useState } from "react";
import { Files, Maximize2, X } from "lucide-react";
import "../styles.css";
import * as OA from "../lib/openai";
import {
  useHostContext,
  useIntrinsicHeight,
  usePrivateCapabilities,
  useTheme,
  useToolInput,
  useToolOutput,
} from "../lib/hooks";
import { parseAnyDiff, type FileDiff } from "../lib/diff";
import {
  ApprovalView,
  DiffViewer,
  EmptyState,
  Notice,
  SurfaceHeader,
  WidgetShell,
  resolveSurface,
  type ApprovalRequest,
} from "../widget-ui";
import {
  requestFocusedObject,
  requestCloseFocusedObject,
  requestTargetedReply,
} from "../lib/private-openai";
import { mountWidget } from "../lib/mount";
import { useApprovalDecision } from "./use-approval-decision";

function App() {
  useTheme();
  const host = useHostContext();
  const privateCapabilities = usePrivateCapabilities();
  const rawInput = useToolInput<any>();
  const output = useToolOutput<any>();
  const [navigationError, setNavigationError] = useState("");
  const [approval, setApproval] = useState<ApprovalRequest | null>(null);
  const decision = useApprovalDecision(approval, {
    onTerminal: () => setApproval(null),
  });
  const viewParams = OA.hostViewParams<any>(host.view);
  const modalInput = viewParams &&
      ("diff" in viewParams || "fileChanges" in viewParams || "changes" in viewParams)
    ? viewParams
    : null;
  const input = modalInput ?? rawInput?.diffInput ?? rawInput?.params ?? rawInput ?? {};
  const requestedSurface = input.presentation === "modal" ? "modal" : undefined;
  const surface = resolveSurface(host.displayMode, host.view, requestedSurface);
  const focused = OA.hostViewParams<{ kind?: string; path?: string }>(host.view);
  const focusedPath = focused?.kind === "diff" ? focused.path : undefined;
  const rawDiff: string = input.diff ?? input.patch ?? output?.diff ?? "";
  const diffTruncated = Boolean(input.diffTruncated ?? output?.diffTruncated);
  const files = useMemo(
    () => resolveFiles(rawDiff, input.fileChanges ?? input.changes ?? output?.fileChanges),
    [input.changes, input.fileChanges, output?.fileChanges, rawDiff],
  );
  const totalAdd = files.reduce((sum, file) => sum + file.adds, 0);
  const totalDel = files.reduce((sum, file) => sum + file.dels, 0);
  const completionLabel = (output?.written ?? input.written) === true
    ? "已写入"
    : (output?.applied ?? input.applied) === true
      ? "已应用"
      : "";
  useIntrinsicHeight([surface, files.length, diffTruncated, navigationError]);

  useEffect(() => {
    if (focusedPath &&
        OA.hostViewIsTombstone(host.view) &&
        privateCapabilities.focusedObject) {
      void requestCloseFocusedObject();
    }
  }, [focusedPath, host.view, privateCapabilities.focusedObject]);

  useEffect(() => {
    // Always poll for a pending approval, even when a previous diff output is
    // already present.  A write_file/apply_patch call parks on the Gateway
    // approval before it ever returns a fresh output, so gating this poll on
    // `!output` would leave the approval invisible forever (the widget just
    // spins).  This mirrors chat.tsx's unconditional polling.
    let stopped = false;
    let timer: number | undefined;
    const refresh = async () => {
      try {
        const status = await OA.callTool<any>("execution_status", {});
        if (stopped) return;
        const pending = (status?.approvals ?? [])[0] ?? null;
        setApproval(pending);
        timer = window.setTimeout(refresh, status?.pending ? 1000 : 3500);
      } catch {
        if (!stopped) timer = window.setTimeout(refresh, 2000);
      }
    };
    timer = window.setTimeout(refresh, 100);
    return () => {
      stopped = true;
      if (timer) window.clearTimeout(timer);
    };
  }, []);

  if (approval) {
    return (
      <WidgetShell surface={surface} className="diff-widget">
        <SurfaceHeader
          icon={<Files aria-hidden="true" />}
          title="文件操作等待审批"
          description={approval.source === "appserver" ? "App Server 原生请求" : "Gateway 安全补位"}
        />
        <div className="surface-body">
          <ApprovalView
            request={approval}
            answers={decision.answers}
            onAnswer={decision.answer}
          />
          {decision.error && <Notice tone="danger">{decision.error}</Notice>}
          <div className="embedded-approval-actions">
            <button type="button" className="widget-button widget-button-primary" disabled={decision.busy || !decision.valid} onClick={() => void decision.reply("accept")}>允许一次</button>
            <button type="button" className="widget-button widget-button-secondary" disabled={decision.busy} onClick={() => void decision.reply("decline")}>拒绝</button>
          </div>
        </div>
      </WidgetShell>
    );
  }

  if (!rawInput && !output) {
    return (
      <WidgetShell surface="inline">
        <div className="widget-skeleton">正在准备改动摘要</div>
      </WidgetShell>
    );
  }

  async function openDetails() {
    setNavigationError("");
    const result = await OA.requestModalOrFullscreen("ui://widget/diff.html", {
      presentation: "modal",
      diff: rawDiff,
      fileChanges: input.fileChanges ?? input.changes ?? output?.fileChanges,
      applied: output?.applied,
      written: output?.written,
      diffTruncated,
    });
    if (result === "unavailable") setNavigationError("当前宿主无法打开完整改动视图。");
  }

  if (surface === "inline") {
    return (
      <WidgetShell surface="inline">
        <SurfaceHeader
          icon={<Files aria-hidden="true" />}
          title={`修改 ${files.length} 个文件`}
          description={
            <>
              {totalAdd > 0 && <span className="diff-add">+{totalAdd}</span>}
              {totalAdd > 0 && totalDel > 0 && " · "}
              {totalDel > 0 && <span className="diff-del">−{totalDel}</span>}
              {completionLabel && ` · ${completionLabel}`}
            </>
          }
          actions={files.length > 0 && (
            <button type="button" className="widget-button widget-button-secondary" onClick={openDetails}>
              <Maximize2 aria-hidden="true" />查看改动
            </button>
          )}
        />
        <div className="inline-file-summary">
          {files.slice(0, 3).map((file) => (
            <div className="inline-file-row" key={file.path}>
              <span title={file.path}>{file.path}</span>
              <span className="diff-file-stats">
                {file.adds > 0 && <span className="diff-add">+{file.adds}</span>}
                {file.dels > 0 && <span className="diff-del">−{file.dels}</span>}
              </span>
            </div>
          ))}
          {files.length > 3 && <div className="inline-more">另有 {files.length - 3} 个文件</div>}
          {!files.length && <EmptyState title="没有改动内容" />}
          {diffTruncated && <Notice tone="warning">改动较大，仅显示前 200,000 个字符。</Notice>}
          {navigationError && <Notice tone="danger" role="alert">{navigationError}</Notice>}
        </div>
      </WidgetShell>
    );
  }

  return (
    <WidgetShell
      surface={surface}
      className={`diff-widget${focusedPath ? " focused-object-widget" : ""}`}
    >
      <SurfaceHeader
        icon={<Files aria-hidden="true" />}
        title={`文件改动 · ${files.length}`}
        description={`${totalAdd} 行新增 · ${totalDel} 行删除`}
        actions={focusedPath && privateCapabilities.focusedObject ? (
          <button
            type="button"
            className="widget-icon-button"
            onClick={() => void requestCloseFocusedObject()}
          >
            <X aria-hidden="true" />关闭
          </button>
        ) : undefined}
      />
      <div className="surface-body">
        <DiffViewer
          files={files}
          initialPath={focusedPath}
          onTarget={privateCapabilities.targetedReply
            ? (file) => void requestTargetedReply(`请针对文件 ${file.path} 的改动继续分析。`)
            : undefined}
          onFocus={privateCapabilities.focusedObject && !focusedPath
            ? (file) => void requestFocusedObject(file.path, {
                kind: "diff",
                path: file.path,
              })
            : undefined}
        />
        {diffTruncated && <Notice tone="warning">改动较大，仅显示前 200,000 个字符。</Notice>}
      </div>
    </WidgetShell>
  );
}

function resolveFiles(rawDiff: string, changes: unknown): FileDiff[] {
  if (rawDiff) return parseAnyDiff(rawDiff);
  if (Array.isArray(changes)) {
    return changes.map((path) => ({
      path: String(path),
      kind: "update",
      lines: [],
      adds: 0,
      dels: 0,
    }));
  }
  if (changes && typeof changes === "object") {
    return Object.entries(changes).map(([path, value]: [string, any]) => ({
      path,
      kind: value?.kind ?? "update",
      lines: [],
      adds: 0,
      dels: 0,
    }));
  }
  return [];
}

mountWidget(<App />);
