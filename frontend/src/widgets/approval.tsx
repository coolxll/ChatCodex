import { useState } from "react";
import { Loader2, ShieldAlert } from "lucide-react";
import "../styles.css";
import * as OA from "../lib/openai";
import {
  useHostContext,
  useIntrinsicHeight,
  useTheme,
  useToolInput,
} from "../lib/hooks";
import {
  ApprovalView,
  Notice,
  SurfaceFooter,
  SurfaceHeader,
  WidgetShell,
  approvalKindLabel,
  approvalRiskLabel,
  approvalTitle,
  resolveSurface,
  type ApprovalRequest,
} from "../widget-ui";
import { mountWidget } from "../lib/mount";
import { useApprovalDecision } from "./use-approval-decision";

function App() {
  useTheme();
  const host = useHostContext();
  const rawInput = useToolInput<any>();
  const viewParams = OA.hostViewParams<any>(host.view);
  const request = normalizeRequest(viewParams?.request ?? rawInput);
  const requestedSurface = viewParams?.presentation === "modal" ||
      rawInput?.presentation === "modal" ||
      rawInput?.params?.presentation === "modal"
    ? "modal"
    : undefined;
  const surface = resolveSurface(host.displayMode, host.view, requestedSurface);
  const [navigationError, setNavigationError] = useState("");
  const decision = useApprovalDecision(request, {
    onTerminal(terminal) {
      if (terminal === "resolved") {
        window.setTimeout(() => void OA.requestClose(), 450);
      }
    },
  });
  useIntrinsicHeight([
    surface,
    Boolean(request),
    decision.answers,
    decision.remember,
    decision.error,
    decision.terminal,
    navigationError,
  ]);

  if (!request) {
    return (
      <WidgetShell surface="inline">
        <div className="widget-skeleton"><Loader2 aria-hidden="true" className="animate-spin" />等待加载审批内容</div>
      </WidgetShell>
    );
  }

  async function openDecision() {
    setNavigationError("");
    const result = await OA.requestModalOrFullscreen("ui://widget/approval.html", {
      presentation: "modal",
      request,
    });
    if (result === "unavailable") {
      setNavigationError("当前宿主无法打开审批界面。");
    }
  }

  if (decision.terminal) {
    return (
      <WidgetShell surface={surface}>
        <SurfaceHeader
          icon={<ShieldAlert aria-hidden="true" />}
          title={decision.terminal === "resolved" ? "审批已处理" : "审批已过期"}
          description={decision.terminal === "resolved"
            ? "决定已发送给 Codex。"
            : "该请求已被处理或不再有效。"}
        />
      </WidgetShell>
    );
  }

  if (surface === "inline") {
    return (
      <WidgetShell surface="inline">
        <SurfaceHeader
          icon={<ShieldAlert aria-hidden="true" />}
          title="Codex 正在等待审批"
          description={approvalKindLabel(request)}
          actions={
            <button type="button" className="widget-button widget-button-primary" onClick={openDecision}>
              查看并决定
            </button>
          }
        />
        {navigationError && (
          <div className="surface-body">
            <Notice tone="danger" role="alert">{navigationError}</Notice>
          </div>
        )}
      </WidgetShell>
    );
  }

  return (
    <WidgetShell surface={surface} className="approval-widget">
      <SurfaceHeader
        icon={<ShieldAlert aria-hidden="true" />}
        title={approvalTitle(request)}
        description={`${approvalKindLabel(request)} · ${approvalRiskLabel(request)}`}
      />
      <div className="surface-body">
        <ApprovalView
          request={request}
          answers={decision.answers}
          onAnswer={decision.answer}
          onOpenUrl={(url) => void OA.openExternal(url).catch((cause) =>
            setNavigationError(cause instanceof Error ? cause.message : String(cause)))}
        />
        {decision.canRemember && !decision.form && (
          <label className="remember-decision">
            <input
              type="checkbox"
              checked={decision.remember}
              onChange={(event) => decision.setRemember(event.target.checked)}
            />
            本会话内记住此决定
          </label>
        )}
        {(decision.error || navigationError) && (
          <Notice tone="danger" role="alert">
            {decision.error || navigationError}
          </Notice>
        )}
      </div>
      <SurfaceFooter note="请只允许你理解并信任的操作。">
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
          {decision.busy && <Loader2 aria-hidden="true" className="animate-spin" />}
          {decision.form ? "提交并继续" : "允许并继续"}
        </button>
      </SurfaceFooter>
    </WidgetShell>
  );
}

function normalizeRequest(input: any): ApprovalRequest | null {
  if (!input) return null;
  return (input.request ?? input.params?.request ?? input) as ApprovalRequest;
}

mountWidget(<App />);
