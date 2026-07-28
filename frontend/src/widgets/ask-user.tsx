import { useEffect, useMemo, useState } from "react";
import { HelpCircle, Loader2 } from "lucide-react";
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
import {
  Notice,
  QuestionForm,
  SurfaceFooter,
  SurfaceHeader,
  WidgetShell,
  isQuestionAnswered,
  resolveSurface,
  type QuestionAnswers,
  type QuestionField,
} from "../widget-ui";
import {
  requestTargetedReply,
  showToast,
  triggerHaptic,
} from "../lib/private-openai";
import { mountWidget } from "../lib/mount";

interface RawQuestion {
  id: string;
  header?: string;
  question: string;
  is_other?: boolean;
  is_secret?: boolean;
  options?: Array<{ label: string; value?: unknown; description?: string }> | null;
  type?: QuestionField["type"];
  multiple?: boolean;
  required?: boolean;
}

function App() {
  useTheme();
  const host = useHostContext();
  const privateCapabilities = usePrivateCapabilities();
  const rawInput = useToolInput<any>();
  const output = useToolOutput<any>();
  const viewParams = OA.hostViewParams<any>(host.view);
  const modalInput = viewParams?.questions || viewParams?.questionInput
    ? viewParams
    : null;
  const source = modalInput ??
    rawInput?.questionInput ??
    rawInput?.params ??
    rawInput ??
    output ??
    {};
  const requestedSurface = source.presentation === "modal" ? "modal" : undefined;
  const surface = resolveSurface(host.displayMode, host.view, requestedSurface);
  const questions = useMemo(
    () => normalizeQuestions(output?.questions ?? source.questions ?? []),
    [output?.questions, source.questions],
  );
  const savedDraft = OA.widgetState<any>()?.questionDraft ?? {};
  const [answers, setAnswers] = useState<QuestionAnswers>(() =>
    Object.fromEntries(
      Object.entries(savedDraft).filter(([id]) =>
        !questions.find((question) => question.id === id)?.isSecret),
    ),
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [terminal, setTerminal] = useState<"submitted" | "cancelled" | "expired" | null>(null);
  const allAnswered = questions.every((question) =>
    isQuestionAnswered(question, answers[question.id]),
  );
  const simpleInline = questions.length === 1 &&
    Boolean(questions[0].options?.length) &&
    !questions[0].multiple &&
    !questions[0].isSecret;
  const targetedTextAnswer = questions.length === 1 &&
    !questions[0].options?.length &&
    questions[0].type === "string" &&
    !questions[0].isSecret &&
    privateCapabilities.targetedReply;
  useIntrinsicHeight([surface, questions.length, answers, error, terminal]);

  useEffect(() => {
    const status = String(source.status ?? output?.status ?? "").toLowerCase();
    if (source.expired === true || output?.expired === true || status === "expired") {
      setTerminal("expired");
    }
  }, [output?.expired, output?.status, source.expired, source.status]);

  useEffect(() => {
    const safeDraft = Object.fromEntries(
      questions
        .filter((question) => !question.isSecret)
        .map((question) => [question.id, answers[question.id]])
        .filter(([, value]) => value !== undefined),
    );
    OA.setWidgetState({ questionDraft: safeDraft });
  }, [answers, questions]);

  if (!rawInput && !output) {
    return (
      <WidgetShell surface="inline">
        <div className="widget-skeleton"><Loader2 aria-hidden="true" className="animate-spin" />正在准备问题</div>
      </WidgetShell>
    );
  }

  if (terminal) {
    const title = terminal === "submitted"
      ? "回答已提交"
      : terminal === "cancelled"
        ? "已取消回答"
        : "问题已过期";
    const description = terminal === "submitted"
      ? "ChatGPT 会把你的回答交给 Codex 继续处理。"
      : terminal === "cancelled"
        ? "Codex 会继续处理取消结果。"
        : "该请求已被处理或不再有效。";
    return (
      <WidgetShell surface="inline">
        <SurfaceHeader
          icon={<HelpCircle aria-hidden="true" />}
          title={title}
          description={description}
        />
      </WidgetShell>
    );
  }

  if (!questions.length) {
    return (
      <WidgetShell surface="inline">
        <SurfaceHeader
          icon={<HelpCircle aria-hidden="true" />}
          title="没有可回答的问题"
          description="工具没有返回结构化问题。"
        />
      </WidgetShell>
    );
  }

  async function openForm() {
    setError("");
    const result = await OA.requestModalOrFullscreen("ui://widget/ask-user.html", {
      presentation: "modal",
      questions: source.questions ?? output?.questions,
    });
    if (result === "unavailable") setError("当前宿主无法打开回答界面。");
  }

  async function targetQuestion() {
    if (!questions[0] || !privateCapabilities.targetedReply) return;
    const result = await requestTargetedReply(questions[0].question);
    if (result.ok) await OA.requestClose();
  }

  async function submit(action: "accept" | "cancel") {
    if (action === "accept" && !allAnswered) return;
    if (privateCapabilities.haptic) {
      void triggerHaptic(action === "accept" ? "medium" : "warning");
    }
    setBusy(true);
    setError("");
    try {
      const prompt = action === "accept"
        ? [
          "我对刚才问题的回答如下：",
          ...questions.map((question) =>
            `- ${question.question}: ${formatAnswer(answers[question.id]) || "（未回答）"}`),
          "请基于这些回答继续当前任务。",
        ].join("\n")
        : "我取消了刚才的问题。请在不依赖这些信息的情况下继续，或说明为什么无法继续。";
      await OA.sendFollowUpMessage(prompt);
      setTerminal(action === "accept" ? "submitted" : "cancelled");
      OA.setWidgetState({ questionDraft: {} });
      if (privateCapabilities.toast) {
        void showToast({
          level: action === "accept" ? "success" : "warning",
          title: action === "accept" ? "回答已提交" : "已取消回答",
        });
      }
      await OA.requestClose();
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : String(cause);
      if (/missing|expired|already resolved/i.test(message)) setTerminal("expired");
      else setError(message);
    } finally {
      setBusy(false);
    }
  }

  if (surface === "inline") {
    return (
      <WidgetShell surface="inline">
        <SurfaceHeader
          icon={<HelpCircle aria-hidden="true" />}
          title="需要你的输入"
          description={questions.length === 1 ? questions[0].question : `Codex 提出了 ${questions.length} 个问题`}
          actions={!simpleInline && (
            <>
              {targetedTextAnswer && (
                <button type="button" className="widget-button widget-button-secondary" onClick={targetQuestion}>
                  在 ChatGPT 中回答
                </button>
              )}
              <button type="button" className="widget-button widget-button-primary" onClick={openForm}>
                填写答案
              </button>
            </>
          )}
        />
        {simpleInline && (
          <>
            <div className="surface-body">
              <QuestionForm
                questions={questions}
                answers={answers}
                onChange={(id, value) => {
                  if (privateCapabilities.haptic) void triggerHaptic("selection");
                  setAnswers((current) => ({ ...current, [id]: value }));
                }}
              />
              {error && <Notice tone="danger" role="alert">{error}</Notice>}
            </div>
            <SurfaceFooter>
              <button
                type="button"
                className="widget-button widget-button-primary"
                disabled={!allAnswered || busy}
                onClick={() => submit("accept")}
              >
                提交
              </button>
            </SurfaceFooter>
          </>
        )}
        {!simpleInline && error && <div className="surface-body"><Notice tone="danger" role="alert">{error}</Notice></div>}
      </WidgetShell>
    );
  }

  return (
    <WidgetShell surface={surface} className="ask-widget">
      <SurfaceHeader
        icon={<HelpCircle aria-hidden="true" />}
        title="需要你的输入"
        description={`回答 ${questions.length} 个问题后，Codex 会继续当前任务。`}
      />
      <div className="surface-body">
        <QuestionForm
          questions={questions}
          answers={answers}
          onChange={(id, value) => {
            if (privateCapabilities.haptic) void triggerHaptic("selection");
            setAnswers((current) => ({ ...current, [id]: value }));
          }}
        />
        {error && <Notice tone="danger" role="alert">{error}</Notice>}
      </div>
      <SurfaceFooter>
        <button type="button" className="widget-button widget-button-secondary" disabled={busy} onClick={() => submit("cancel")}>
          取消
        </button>
        <button
          type="button"
          className="widget-button widget-button-primary"
          disabled={busy || !allAnswered}
          onClick={() => submit("accept")}
        >
          {busy && <Loader2 aria-hidden="true" className="animate-spin" />}
          提交并继续
        </button>
      </SurfaceFooter>
    </WidgetShell>
  );
}

function normalizeQuestions(questions: RawQuestion[]): QuestionField[] {
  return questions.map((question) => ({
    id: question.id,
    header: question.header,
    question: question.question,
    required: question.required ?? true,
    isOther: Boolean(question.is_other),
    isSecret: Boolean(question.is_secret),
    multiple: Boolean(question.multiple),
    type: question.type ?? "string",
    options: question.options?.map((option) => ({
      label: option.label,
      value: option.value ?? option.label,
      description: option.description,
    })),
  }));
}

function formatAnswer(value: unknown): string {
  if (Array.isArray(value)) return value.map(String).join(", ");
  return value == null ? "" : String(value);
}

mountWidget(<App />);
