import { useEffect, useMemo, useRef, useState } from "react";
import {
  Check,
  Folder,
  Loader2,
  Maximize2,
  ShieldCheck,
  TriangleAlert,
} from "lucide-react";
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
import { DirBrowser } from "../lib/DirBrowser";
import {
  ChoiceList,
  Notice,
  Section,
  SegmentedControl,
  SurfaceFooter,
  SurfaceHeader,
  WidgetShell,
  resolveSurface,
} from "../widget-ui";
import { showToast, triggerHaptic } from "../lib/private-openai";
import { mountWidget } from "../lib/mount";

type WorkMode = "plan" | "agent";
type Sandbox = "read-only" | "workspace-write" | "danger-full-access";
type ApprovalPolicy = "untrusted" | "on-request" | "never";
type ApprovalPolicyKey = ApprovalPolicy | "granular";
type GranularApprovalPolicy = {
  granular: {
    sandbox_approval: boolean;
    rules: boolean;
    skill_approval?: boolean;
    request_permissions?: boolean;
    mcp_elicitations: boolean;
  };
};

interface Requirements {
  allowedApprovalPolicies?: Array<ApprovalPolicy | GranularApprovalPolicy>;
  allowedSandboxModes?: Sandbox[];
}

interface StartDefaults {
  sandbox?: Sandbox;
  approvalPolicy?: ApprovalPolicy;
}

const ACCESS: Array<{ value: Sandbox; label: string; detail: string }> = [
  { value: "read-only", label: "只读", detail: "检查和规划；写入或执行受限制。" },
  { value: "workspace-write", label: "工作区可写", detail: "可修改所选工作区；越界访问仍受限制。" },
  { value: "danger-full-access", label: "完全访问", detail: "不使用文件系统沙箱，可以访问工作区外资源。" },
];

const WORK_MODES: Array<{ value: WorkMode; title: string; description: string }> = [
  { value: "agent", title: "Agent", description: "执行任务、修改代码并验证结果" },
  { value: "plan", title: "Plan", description: "先分析并形成实施计划，支持向你追问" },
];

const APPROVAL_POLICIES: Array<{ value: ApprovalPolicyKey; label: string; detail: string }> = [
  { value: "on-request", label: "按需请求", detail: "独立变更操作由 Gateway 请求一次审批。" },
  { value: "untrusted", label: "严格请求", detail: "仅已知安全的读取操作自动运行。" },
  { value: "never", label: "从不请求", detail: "不弹出审批；被权限阻止的操作直接失败。" },
  { value: "granular", label: "托管细粒度", detail: "遵守 App Server 托管要求中的 sandbox_approval 类别。" },
];

function App() {
  useTheme();
  const host = useHostContext();
  const privateCapabilities = usePrivateCapabilities();
  const output = useToolOutput<any>();
  const suggested = useToolInput<{ cwd?: string; workMode?: WorkMode }>() ?? {};
  const suggestedCwd = suggested.cwd ?? output?.suggestedCwd ?? "";
  const requirements = (output?.requirements ?? null) as Requirements | null;
  const defaults = (output?.defaults ?? null) as StartDefaults | null;
  const defaultsApplied = useRef(false);
  const [cwd, setCwd] = useState(suggestedCwd);
  const [browserRoot, setBrowserRoot] = useState(suggestedCwd);
  const [sandbox, setSandbox] = useState<Sandbox>("workspace-write");
  const [mode, setMode] = useState<WorkMode>("agent");
  const [approvalPolicy, setApprovalPolicy] = useState<ApprovalPolicyKey>("on-request");
  const [showBrowser, setShowBrowser] = useState(false);
  const [riskConfirmed, setRiskConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [started, setStarted] = useState<any>(null);
  const surface = resolveSurface(host.displayMode, host.view);
  const showConfiguration = surface === "fullscreen";

  useIntrinsicHeight([
    surface,
    showBrowser,
    error,
    started,
    mode,
    sandbox,
    approvalPolicy,
    riskConfirmed,
  ]);

  useEffect(() => {
    if (!cwd && suggestedCwd) {
      setCwd(suggestedCwd);
      setBrowserRoot(suggestedCwd);
    }
    const next = suggested.workMode ?? output?.suggestedWorkMode;
    if (next === "plan" || next === "agent") setMode(next);
  }, [cwd, output?.suggestedWorkMode, suggested.workMode, suggestedCwd]);

  useEffect(() => {
    if (!defaults || defaultsApplied.current) return;
    defaultsApplied.current = true;
    if (ACCESS.some((item) => item.value === defaults.sandbox)) setSandbox(defaults.sandbox!);
    if (APPROVAL_POLICIES.some((item) => item.value === defaults.approvalPolicy)) {
      setApprovalPolicy(defaults.approvalPolicy!);
    }
  }, [defaults]);

  useEffect(() => {
    const sandboxes = requirements?.allowedSandboxModes;
    const policies = allowedPolicyKeys(requirements);
    if (sandboxes?.length && !sandboxes.includes(sandbox)) setSandbox(sandboxes[0]);
    if (policies?.length && !policies.includes(approvalPolicy)) setApprovalPolicy(policies[0]);
  }, [approvalPolicy, requirements, sandbox]);

  const ready = started?.conversationId
    ? started
    : output?.conversationId
      ? output
      : null;
  if (ready) {
    return (
      <WidgetShell surface="inline">
        <SurfaceHeader
          icon={<Check aria-hidden="true" />}
          title="执行工作区已连接"
          description="WebChat 可以使用受控的本地执行工具。"
        />
      </WidgetShell>
    );
  }

  const activeAccess = ACCESS.find((item) => item.value === sandbox)!;
  const activePolicy = APPROVAL_POLICIES.find((item) => item.value === approvalPolicy)!;
  const riskyCombination = sandbox === "danger-full-access";
  const policyDisallowed = Boolean(
    requirements?.allowedApprovalPolicies
    && !allowedPolicyKeys(requirements).includes(approvalPolicy),
  );
  const sandboxDisallowed = Boolean(
    requirements?.allowedSandboxModes
    && !requirements.allowedSandboxModes.includes(sandbox),
  );
  const disabledReason = busy
    ? "正在保存"
    : !cwd.trim()
      ? "请选择工作目录"
      : sandboxDisallowed
        ? "托管要求禁止当前沙箱模式"
        : policyDisallowed
          ? "托管要求禁止当前审批策略"
        : riskyCombination && !riskConfirmed
          ? "请确认高风险组合"
          : "";
  const recentPaths = useMemo(
    () => Array.from(new Set([suggestedCwd].filter(Boolean))),
    [suggestedCwd],
  );

  async function openConfiguration() {
    setError("");
    try {
      const actual = await OA.requestDisplayMode("fullscreen");
      if (actual !== "fullscreen") {
        setError("当前宿主不支持完整工作区配置视图。");
      }
    } catch (cause) {
      setError(cause instanceof Error
        ? cause.message
        : "当前宿主不支持完整工作区配置视图。");
    }
  }

  function selectWithFeedback<T>(setter: (value: T) => void, value: T) {
    if (privateCapabilities.haptic) void triggerHaptic("selection");
    setter(value);
  }

  function chooseSandbox(next: Sandbox) {
    selectWithFeedback(setSandbox, next);
    setRiskConfirmed(false);
  }

  function chooseApproval(next: ApprovalPolicyKey) {
    selectWithFeedback(setApprovalPolicy, next);
    setRiskConfirmed(false);
  }

  async function submit() {
    if (disabledReason) {
      setError(disabledReason);
      return;
    }
    if (privateCapabilities.haptic) {
      void triggerHaptic(riskyCombination ? "warning" : "medium");
    }
    setBusy(true);
    setError("");
    try {
      const config: Record<string, unknown> = {
        cwd: cwd || undefined,
        workMode: mode,
        sandbox,
        approvalPolicy: approvalPolicy === "granular"
          ? granularPolicy(requirements)
          : approvalPolicy,
        approvalsReviewer: "user",
      };
      const result = await OA.callTool<any>("save_execution_context", { config });
      if (result?.error) throw new Error(result.message ?? result.error);
      setStarted(result);
      OA.setWidgetState({
        conversationId: result?.conversationId,
        contextId: result?.contextId,
        contextVersion: result?.contextVersion,
        cwd,
        workMode: mode,
        sandbox,
        approvalPolicy: approvalPolicy === "granular"
          ? granularPolicy(requirements)
          : approvalPolicy,
        approvalsReviewer: "user",
        recentDirectories: recentPaths.slice(0, 5),
      });
      if (privateCapabilities.toast) {
        void showToast({
          level: "success",
          title: "执行工作区已连接",
          body: cwd,
        });
      }
      await OA.requestClose();
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : String(cause);
      setError(message);
      if (privateCapabilities.toast) {
        void showToast({ level: "danger", title: "无法保存工作区", body: message });
      }
    } finally {
      setBusy(false);
    }
  }

  if (!showConfiguration) {
    return (
      <WidgetShell surface="inline" className="start-widget">
        <SurfaceHeader
          icon={<Folder aria-hidden="true" />}
          title="打开 Codex 工作区"
          description="确认建议配置后，在完整视图中选择目录和权限。"
        />
        <div className="surface-body inline-configuration-summary">
          <div className="summary-row">
            <span>建议目录</span>
            <code title={suggestedCwd || "未提供"}>{suggestedCwd || "尚未选择"}</code>
          </div>
          <div className="summary-row">
            <span>默认访问</span>
            <strong>{activeAccess.label}</strong>
          </div>
          {error && <Notice tone="danger" role="alert">{error}</Notice>}
        </div>
        <SurfaceFooter>
          <button type="button" className="widget-button widget-button-secondary" onClick={() => OA.requestClose()}>
            取消
          </button>
          <button type="button" className="widget-button widget-button-primary" onClick={openConfiguration}>
            <Maximize2 aria-hidden="true" />配置并打开
          </button>
        </SurfaceFooter>
      </WidgetShell>
    );
  }

  return (
    <WidgetShell surface={surface} className="start-widget">
      <SurfaceHeader
        icon={<Folder aria-hidden="true" />}
        title="打开执行工作区"
        description="选择目录和本地访问权限；不会创建 Codex 会话。"
      />
      <div className="surface-body start-grid">
        <div className="start-pane">
          <Section title="工作目录">
              <div className="widget-field">
                <Folder aria-hidden="true" />
                <input
                  value={cwd}
                  aria-label="工作目录"
                  onChange={(event) => setCwd(event.target.value)}
                  placeholder="E:\\project 或 ~/code/app"
                  spellCheck={false}
                />
                {surface === "fullscreen" && (
                  <button
                    type="button"
                    className="widget-icon-button"
                    onClick={() => {
                      setBrowserRoot(cwd);
                      setShowBrowser((current) => !current);
                    }}
                  >
                    {showBrowser ? "收起" : "浏览"}
                  </button>
                )}
              </div>
              {showBrowser && surface === "fullscreen" && (
                <DirBrowser
                  initialPath={browserRoot}
                  selectedPath={cwd}
                  recentPaths={recentPaths}
                  onSelect={setCwd}
                />
              )}
          </Section>
        </div>

        <div className="start-pane start-safety">
          <Section title="协作模式">
            <ChoiceList
              label="协作模式"
              value={mode}
              choices={WORK_MODES.map((item) => ({
                value: item.value,
                title: item.title,
                description: item.description,
              }))}
              onChange={(value) => selectWithFeedback(setMode, value)}
            />
          </Section>

          <Section title="系统访问">
            <SegmentedControl
              label="系统访问"
              value={sandbox}
              options={ACCESS.map((item) => ({
                value: item.value,
                label: item.label,
                disabled: requirements?.allowedSandboxModes
                  ? !requirements.allowedSandboxModes.includes(item.value)
                  : false,
              }))}
              onChange={chooseSandbox}
            />
            <Notice tone={sandbox === "danger-full-access" ? "warning" : "info"}>
              {sandbox === "danger-full-access"
                ? <TriangleAlert aria-hidden="true" className="sr-only" />
                : <ShieldCheck aria-hidden="true" className="sr-only" />}
              {activeAccess.detail}
            </Notice>
          </Section>

          <Section title="请求审批">
            <SegmentedControl
              label="审批策略"
              value={approvalPolicy}
              options={APPROVAL_POLICIES.map((item) => ({
                value: item.value,
                label: item.label,
                disabled: item.value === "granular"
                  ? !allowedPolicyKeys(requirements).includes("granular")
                  : requirements?.allowedApprovalPolicies
                    ? !allowedPolicyKeys(requirements).includes(item.value)
                    : false,
              }))}
              onChange={chooseApproval}
            />
            <Notice>{activePolicy.detail} 审批决定始终由你本人作出。</Notice>
          </Section>

          {riskyCombination && (
            <label className="risk-confirmation">
              <input
                type="checkbox"
                checked={riskConfirmed}
                onChange={(event) => {
                  if (privateCapabilities.haptic) void triggerHaptic("warning");
                  setRiskConfirmed(event.target.checked);
                }}
              />
              <span>
                <strong>确认完全访问</strong>
                <small>获批的命令可以访问工作区外资源。只在你完全信任当前任务时继续。</small>
              </span>
            </label>
          )}
        </div>

        {error && <div className="start-grid-error"><Notice tone="danger" role="alert">{error}</Notice></div>}
      </div>
      <SurfaceFooter note={disabledReason || "协作模式不改变系统访问和审批策略。"}>
        <button type="button" className="widget-button widget-button-secondary" disabled={busy} onClick={() => OA.requestClose()}>
          取消
        </button>
        <button
          type="button"
          className="widget-button widget-button-primary"
          disabled={Boolean(disabledReason)}
          title={disabledReason || undefined}
          onClick={submit}
        >
          {busy && <Loader2 aria-hidden="true" className="animate-spin" />}
          {busy ? "正在连接" : "开始"}
        </button>
      </SurfaceFooter>
    </WidgetShell>
  );
}

function allowedPolicyKeys(
  requirements: Requirements | null,
): ApprovalPolicyKey[] {
  return (requirements?.allowedApprovalPolicies ?? []).flatMap((policy) => {
    if (typeof policy === "string") {
      return APPROVAL_POLICIES.some((item) => item.value === policy)
        ? [policy]
        : [];
    }
    return policy?.granular ? ["granular"] : [];
  });
}

function granularPolicy(
  requirements: Requirements | null,
): GranularApprovalPolicy | null {
  return requirements?.allowedApprovalPolicies?.find(
    (policy): policy is GranularApprovalPolicy =>
      typeof policy === "object" && policy !== null && "granular" in policy,
  ) ?? null;
}

mountWidget(<App />);
