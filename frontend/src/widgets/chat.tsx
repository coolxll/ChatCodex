import { useEffect, useRef, useState } from "react";
import {
  Bot,
  Loader2,
  Maximize2,
  ShieldAlert,
} from "lucide-react";
import "../styles.css";
import * as OA from "../lib/openai";
import {
  useHostContext,
  useIntrinsicHeight,
  useTheme,
  useToolOutput,
} from "../lib/hooks";
import {
  ApprovalView,
  CodeBlock,
  EmptyState,
  Notice,
  Section,
  StatusRow,
  SurfaceHeader,
  WidgetShell,
  approvalRiskLabel,
  resolveSurface,
  type ApprovalRequest,
} from "../widget-ui";
import { mountWidget } from "../lib/mount";
import { useApprovalDecision } from "./use-approval-decision";

interface ExecutionStatus {
  conversationId?: string;
  contextId?: string;
  contextVersion?: number;
  status?: string;
  pending?: boolean;
  approvals?: ApprovalRequest[];
  context?: {
    cwd?: string;
    sandboxMode?: string;
    approvalPolicy?: string;
    workMode?: string;
    version?: number;
  };
  capabilities?: {
    appServerMode?: string;
    execPolicyMode?: string;
    fallbackApproval?: boolean;
    codexAgentSessions?: boolean;
    standaloneFilesystem?: string;
    remoteFilesystemBoundary?: string;
  };
  exitCode?: number;
  stdout?: string;
  stderr?: string;
}

function App() {
  useTheme();
  const host = useHostContext();
  const output = useToolOutput<ExecutionStatus>();
  const persisted = OA.widgetState<ExecutionStatus>() ?? {};
  const [snapshot, setSnapshot] = useState<ExecutionStatus>(
    output ?? persisted,
  );
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<ApprovalRequest | null>(null);
  const outputRef = useRef(output);
  const surface = resolveSurface(host.displayMode, host.view);
  const approvals = snapshot.approvals ?? [];
  const context = snapshot.context ?? {};
  const conversationId = (
    snapshot.conversationId
    ?? output?.conversationId
    ?? persisted.conversationId
    ?? ""
  );

  useIntrinsicHeight([
    surface,
    approvals.length,
    selected?.requestId,
    snapshot.exitCode,
    error,
  ]);

  useEffect(() => {
    if (!output || output === outputRef.current) return;
    outputRef.current = output;
    setSnapshot((current) => ({
      ...current,
      ...output,
      conversationId: output.conversationId ?? current.conversationId,
      context: output.context ?? current.context,
      approvals: output.approvals ?? current.approvals,
    }));
  }, [output]);

  useEffect(() => {
    let stopped = false;
    let timer: number | undefined;

    const refresh = async () => {
      try {
        const next = await OA.callTool<ExecutionStatus>(
          "execution_status",
          conversationId ? { conversationId } : {},
        );
        if (stopped) return;
        setSnapshot((current) => ({
          ...current,
          ...next,
          exitCode: current.exitCode,
          stdout: current.stdout,
          stderr: current.stderr,
        }));
        setError("");
        OA.setWidgetState({
          conversationId: next.conversationId,
          contextId: next.contextId,
          contextVersion: next.contextVersion,
          context: next.context,
          capabilities: next.capabilities,
        });
        timer = window.setTimeout(refresh, next.pending ? 900 : 3500);
      } catch (cause) {
        if (stopped) return;
        setError(cause instanceof Error ? cause.message : String(cause));
        timer = window.setTimeout(refresh, 4000);
      }
    };

    timer = window.setTimeout(refresh, 150);
    return () => {
      stopped = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [conversationId]);

  async function openDetails() {
    try {
      await OA.requestDisplayMode("fullscreen");
    } catch {
      setError("当前宿主无法打开完整执行视图。");
    }
  }

  const primary = approvals[0] ?? null;
  const title = primary
    ? "本地操作等待审批"
    : snapshot.exitCode === 0
      ? "命令执行完成"
      : typeof snapshot.exitCode === "number"
        ? "命令执行失败"
        : "执行工作区已就绪";
  const description = primary
    ? approvalSummary(primary)
    : conversationId
      ? `WebChat 执行上下文 · v${snapshot.contextVersion ?? context.version ?? 1}`
      : "正在读取当前 WebChat 执行上下文";

  if (surface === "inline") {
    return (
      <WidgetShell surface="inline" className="chat-widget">
        <SurfaceHeader
          icon={primary
            ? <ShieldAlert aria-hidden="true" />
            : <Bot aria-hidden="true" />}
          title={title}
          description={description}
          actions={
            <button
              type="button"
              className="widget-icon-button"
              onClick={openDetails}
            >
              <Maximize2 aria-hidden="true" />详情
            </button>
          }
        />
        <div className="surface-body activity-summary">
          {primary ? (
            <EmbeddedApproval
              request={primary}
              onResolved={() => removeApproval(primary.requestId)}
            />
          ) : (
            <CommandResult snapshot={snapshot} />
          )}
          {approvals.slice(1).map((request) => (
            <StatusRow
              key={request.requestId}
              tone="warning"
              title={approvalSummary(request)}
              detail={approvalOwnerLabel(request)}
            />
          ))}
          {error && (
            <Notice tone="danger" role="alert">
              状态刷新失败：{error}
            </Notice>
          )}
        </div>
      </WidgetShell>
    );
  }

  return (
    <WidgetShell surface={surface} className="chat-widget">
      <SurfaceHeader
        icon={<Bot aria-hidden="true" />}
        title={title}
        description={description}
      />
      <div className="surface-body chat-fullscreen-grid">
        <section className="activity-timeline" aria-label="独立操作审批">
          <div className="timeline-heading">
            <h2>审批队列</h2>
            <span>{approvals.length ? `${approvals.length} 项待处理` : "空闲"}</span>
          </div>
          {approvals.length ? approvals.map((request) => (
            <StatusRow
              key={request.requestId}
              tone="warning"
              title={approvalSummary(request)}
              detail={approvalOwnerLabel(request)}
              actions={
                <button
                  type="button"
                  className="widget-icon-button"
                  onClick={() => setSelected(request)}
                >
                  查看
                </button>
              }
            />
          )) : (
            <EmptyState title="当前没有待审批操作" />
          )}
          <CommandResult snapshot={snapshot} />
          {error && <Notice tone="danger" role="alert">{error}</Notice>}
        </section>

        <aside className="run-inspector">
          {selected ? (
            <EmbeddedApproval
              request={selected}
              onResolved={() => {
                removeApproval(selected.requestId);
                setSelected(null);
              }}
            />
          ) : (
            <>
              <Section title="执行上下文">
                <div className="inspector-facts">
                  <Fact label="对话" value={compactId(conversationId)} />
                  <Fact label="目录" value={context.cwd ?? "未配置"} sensitive />
                  <Fact label="工作模式" value={workModeLabel(context.workMode)} />
                  <Fact label="沙箱" value={sandboxLabel(context.sandboxMode)} />
                  <Fact
                    label="审批策略"
                    value={approvalPolicyLabel(context.approvalPolicy)}
                  />
                </div>
              </Section>
              <Section title="运行能力">
                <div className="inspector-facts">
                  <Fact
                    label="App Server"
                    value={snapshot.capabilities?.appServerMode ?? "未知"}
                  />
                  <Fact
                    label="exec-policy"
                    value={snapshot.capabilities?.execPolicyMode ?? "未知"}
                  />
                  <Fact label="Gateway 补位" value="已启用" />
                  <Fact
                    label="独立文件 RPC"
                    value={snapshot.capabilities?.standaloneFilesystem ?? "未知"}
                  />
                  <Fact label="Codex agent 会话" value="已禁用" />
                </div>
              </Section>
            </>
          )}
        </aside>
      </div>
    </WidgetShell>
  );

  function removeApproval(requestId?: string) {
    setSnapshot((current) => ({
      ...current,
      approvals: (current.approvals ?? []).filter(
        (request) => request.requestId !== requestId,
      ),
      pending: false,
    }));
  }
}

function EmbeddedApproval({
  request,
  onResolved,
}: {
  request: ApprovalRequest;
  onResolved(): void;
}) {
  const decision = useApprovalDecision(request, { onTerminal: onResolved });

  return (
    <div className="embedded-approval">
      <div className="embedded-approval-heading">
        <ShieldAlert aria-hidden="true" />
        <h2>{approvalOwnerLabel(request)}</h2>
        <span>{approvalRiskLabel(request)}</span>
      </div>
      <ApprovalView
        request={request}
        answers={decision.answers}
        onAnswer={decision.answer}
        onOpenUrl={(url) => void OA.openExternal(url)}
      />
      {decision.error && (
        <Notice tone="danger" role="alert">{decision.error}</Notice>
      )}
      <div className="embedded-approval-actions">
        <button
          type="button"
          className="widget-button widget-button-secondary"
          disabled={decision.busy}
          onClick={() => decision.reply(decision.form ? "cancel" : "decline")}
        >
          {decision.form ? "取消" : "拒绝"}
        </button>
        <button
          type="button"
          className="widget-button widget-button-primary"
          disabled={decision.busy || (decision.form && !decision.valid)}
          onClick={() => decision.reply("accept")}
        >
          {decision.busy && (
            <Loader2 aria-hidden="true" className="animate-spin" />
          )}
          {decision.form ? "提交并继续" : "仅允许这一次"}
        </button>
      </div>
    </div>
  );
}

function CommandResult({ snapshot }: { snapshot: ExecutionStatus }) {
  if (typeof snapshot.exitCode !== "number") {
    return (
      <StatusRow
        tone="neutral"
        title="独立执行通道就绪"
        detail="命令只会在审批通过后交给 App Server"
      />
    );
  }
  const succeeded = snapshot.exitCode === 0;
  const output = [snapshot.stdout, snapshot.stderr].filter(Boolean).join("\n");
  return (
    <>
      <StatusRow
        tone={succeeded ? "success" : "danger"}
        title={succeeded ? "命令执行完成" : "命令执行失败"}
        detail={`退出码 ${snapshot.exitCode}`}
      />
      {output && (
        <CodeBlock label="命令输出" collapsed>
          {output}
        </CodeBlock>
      )}
    </>
  );
}

function approvalSummary(request: ApprovalRequest) {
  return ({
    commandExecution: "运行命令",
    fileChange: "修改文件",
    permissions: "扩大本地权限",
    network: "访问网络",
    userInput: "回答问题",
    elicitation: "工具请求输入",
  } as Record<string, string>)[request.kind ?? ""] ?? "确认操作";
}

function approvalOwnerLabel(request: ApprovalRequest) {
  return request.source === "gateway"
    ? "Gateway 安全补位请求"
    : "Codex App Server 原生请求";
}

function compactId(value: string) {
  if (!value) return "—";
  return value.length > 18
    ? `${value.slice(0, 8)}…${value.slice(-6)}`
    : value;
}

function workModeLabel(value?: string) {
  return value === "plan" ? "Plan（禁止写入）" : "Agent";
}

function sandboxLabel(value?: string) {
  return ({
    "read-only": "只读",
    "workspace-write": "工作区可写",
    "danger-full-access": "完全访问",
  } as Record<string, string>)[value ?? ""] ?? "未记录";
}

function approvalPolicyLabel(value?: string) {
  return ({
    "on-request": "按需单次审批",
    untrusted: "严格单次审批",
    never: "独立副作用操作禁止",
  } as Record<string, string>)[value ?? ""] ?? "未记录";
}

function Fact({
  label,
  value,
  sensitive = false,
}: {
  label: string;
  value: string;
  sensitive?: boolean;
}) {
  if (sensitive) {
    return (
      <details className="inspector-fact sensitive">
        <summary><span>{label}</span><strong>显示</strong></summary>
        <code>{value}</code>
      </details>
    );
  }
  return (
    <div className="inspector-fact">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

mountWidget(<App />);
