import { useEffect, useMemo, useRef, useState } from "react";
import * as OA from "../lib/openai";
import { usePrivateCapabilities } from "../lib/hooks";
import { showToast, triggerHaptic } from "../lib/private-openai";
import {
  approvalCanRemember,
  approvalIsForm,
  approvalQuestions,
  approvalReplyPayload,
  isQuestionAnswered,
  type ApprovalRequest,
  type QuestionAnswers,
} from "../widget-ui";

export type ApprovalAction = "accept" | "decline" | "cancel";
export type ApprovalTerminal = "resolved" | "expired";

interface ApprovalDecisionOptions {
  onTerminal?(terminal: ApprovalTerminal): void;
}

export function useApprovalDecision(
  request: ApprovalRequest | null,
  options: ApprovalDecisionOptions = {},
) {
  const privateCapabilities = usePrivateCapabilities();
  const onTerminalRef = useRef(options.onTerminal);
  const [answers, setAnswers] = useState<QuestionAnswers>({});
  const [remember, setRemember] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [terminal, setTerminal] = useState<ApprovalTerminal | null>(null);
  const requestKey = `${request?.requestId ?? ""}:${request?.kind ?? ""}`;
  const questions = useMemo(
    () => request ? approvalQuestions(request) : [],
    [request],
  );
  const form = request ? approvalIsForm(request) : false;
  const canRemember = request ? approvalCanRemember(request) : false;
  const valid = questions.every((question) =>
    isQuestionAnswered(question, answers[question.id]),
  );

  onTerminalRef.current = options.onTerminal;

  useEffect(() => {
    setAnswers({});
    setRemember(false);
    setBusy(false);
    setError("");
    setTerminal(null);
  }, [requestKey]);

  function answer(id: string, value: unknown) {
    if (privateCapabilities.haptic) void triggerHaptic("selection");
    setAnswers((current) => ({ ...current, [id]: value }));
  }

  async function reply(action: ApprovalAction): Promise<boolean> {
    if (!request || busy || (form && action === "accept" && !valid)) return false;
    if (privateCapabilities.haptic) {
      void triggerHaptic(action === "accept" ? "medium" : "warning");
    }
    setBusy(true);
    setError("");
    try {
      await OA.callTool(
        "resolve_approval",
        approvalReplyPayload(request, action, answers, remember),
      );
      setTerminal("resolved");
      if (privateCapabilities.toast) {
        void showToast({
          level: action === "accept" ? "success" : "warning",
          title: action === "accept" ? "审批已提交" : "操作已拒绝",
        });
      }
      onTerminalRef.current?.("resolved");
      return true;
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : String(cause);
      if (/missing|expired|already resolved/i.test(message)) {
        setTerminal("expired");
        onTerminalRef.current?.("expired");
      } else {
        setError(message);
      }
      return false;
    } finally {
      setBusy(false);
    }
  }

  return {
    answers,
    answer,
    remember,
    setRemember,
    busy,
    error,
    terminal,
    questions,
    form,
    canRemember,
    valid,
    reply,
  };
}
