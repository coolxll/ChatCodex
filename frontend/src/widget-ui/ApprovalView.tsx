import React, { useMemo } from "react";
import { ExternalLink, Globe2, ShieldAlert, Terminal, Wrench } from "lucide-react";
import { parseAnyDiff, type FileDiff } from "../lib/diff";
import { CodeBlock } from "./CodeBlock";
import { DiffViewer } from "./DiffViewer";
import { Notice } from "./Notice";
import {
  QuestionForm,
  type QuestionAnswers,
  type QuestionField,
} from "./QuestionForm";
import { Section } from "./Section";

export interface ApprovalRequest {
  requestId?: string;
  kind?: string;
  method?: string;
  params?: Record<string, any>;
  summary?: string;
  reason?: string;
  [key: string]: any;
}

export function approvalParams(request: ApprovalRequest): Record<string, any> {
  return request.params && typeof request.params === "object"
    ? { ...request, ...request.params }
    : request;
}

export function approvalTitle(request: ApprovalRequest): string {
  const kind = request.kind ?? "";
  const actor = request.source === "gateway" ? "Gateway" : "App Server";
  return ({
    commandExecution: `允许 ${actor} 运行命令？`,
    fileChange: `允许 ${actor} 修改文件？`,
    permissions: "允许额外的本地访问？",
    network: "允许网络访问？",
    userInput: "Codex 需要你的输入",
    elicitation: "连接的工具需要你的输入",
  } as Record<string, string>)[kind] ?? `确认 ${actor} 操作`;
}

export function approvalKindLabel(request: ApprovalRequest): string {
  return ({
    commandExecution: "运行命令",
    fileChange: "修改文件",
    permissions: "扩大权限",
    network: "访问网络",
    userInput: "回答问题",
    elicitation: "工具请求输入",
  } as Record<string, string>)[request.kind ?? ""] ?? "确认操作";
}

export function approvalRiskLabel(request: ApprovalRequest): string {
  const params = approvalParams(request);
  if (params.risk === "high" || params.destructive === true) return "高风险";
  if (request.kind === "commandExecution" ||
      request.kind === "permissions" ||
      request.kind === "network") {
    return "需要谨慎确认";
  }
  return "一般确认";
}

export function approvalQuestions(request: ApprovalRequest): QuestionField[] {
  const params = approvalParams(request);
  const schema = params.requestedSchema ?? {};
  const required = new Set<string>(schema.required ?? []);
  if (Array.isArray(params.questions)) {
    return params.questions.map((question: any, index: number) => ({
      id: String(question.id ?? `question_${index + 1}`),
      header: question.header,
      question: String(question.question ?? question.header ?? "请输入"),
      required: question.required ?? true,
      isOther: Boolean(question.is_other ?? question.isOther),
      isSecret: Boolean(question.is_secret ?? question.isSecret),
      multiple: Boolean(question.multiple),
      type: question.type ?? "string",
      options: question.options?.map((option: any) => ({
        label: String(option.label ?? option.value),
        value: option.value ?? option.label,
        description: option.description,
      })),
    }));
  }
  return Object.entries(schema.properties ?? {}).map(([id, value]: [string, any]) => ({
    id,
    header: value.title,
    question: String(value.description ?? value.title ?? id),
    required: required.has(id),
    isSecret: value.format === "password",
    multiple: value.type === "array",
    type: value.type,
    minimum: value.minimum,
    maximum: value.maximum,
    minLength: value.minLength,
    maxLength: value.maxLength,
    options: schemaOptions(value),
  }));
}

export function approvalCanRemember(request: ApprovalRequest): boolean {
  const params = approvalParams(request);
  const decisions = params.availableDecisions ?? [];
  return Boolean(
    params.canRemember ||
    decisions.includes("acceptForSession") ||
    decisions.includes("always"),
  );
}

export function approvalIsForm(request: ApprovalRequest): boolean {
  return request.kind === "userInput" || request.kind === "elicitation";
}

export function approvalReplyPayload(
  request: ApprovalRequest,
  action: "accept" | "decline" | "cancel" | "always",
  answers: QuestionAnswers,
  remember: boolean,
): Record<string, unknown> {
  const params = approvalParams(request);
  const decisions = Array.isArray(params.availableDecisions)
    ? params.availableDecisions.map(String)
    : [];
  let upstreamAction: string = (
    remember && action === "accept" ? "always" : action
  );
  if (action === "accept" && request.source === "gateway") {
    upstreamAction = "approve_once";
  } else if (action === "accept" && !decisions.includes(upstreamAction)) {
    upstreamAction = decisions.find((value: string) =>
      ["accept", "approve", "approve_once", "allow"].includes(value),
    ) ?? upstreamAction;
  }
  const payload: Record<string, unknown> = {
    requestId: request.requestId ?? "",
    action: upstreamAction,
    expectedVersion: request.version,
  };
  if (request.kind === "permissions" && (action === "accept" || action === "always")) {
    payload.permissions = params.permissions && typeof params.permissions === "object"
      ? Object.fromEntries(
          Object.entries(params.permissions).filter(
            ([key]) => answers[permissionAnswerKey(key)] !== false,
          ),
        )
      : {};
    payload.scope = remember ? "session" : "turn";
  }
  if (request.kind === "userInput") {
    payload.answers = Object.fromEntries(
      Object.entries(answers)
        .filter(([, value]) => !isEmpty(value))
        .map(([id, value]) => [
          id,
          { answers: Array.isArray(value) ? value.map(String) : [String(value)] },
        ]),
    );
  }
  if (request.kind === "elicitation" && params.mode !== "url") {
    payload.content = Object.fromEntries(
      Object.entries(answers).filter(([, value]) => !isEmpty(value)),
    );
  }
  return payload;
}

export function ApprovalView({
  request,
  answers,
  onAnswer,
  onOpenUrl,
}: {
  request: ApprovalRequest;
  answers: QuestionAnswers;
  onAnswer(id: string, value: unknown): void;
  onOpenUrl?(url: string): void;
}) {
  const params = approvalParams(request);
  const questions = approvalQuestions(request);
  const files = useMemo(
    () => approvalFiles(params.diff, params.fileChanges, params.patch),
    [params.diff, params.fileChanges, params.patch],
  );
  const reason = params.reason ?? params.message ?? params.summary;

  return (
    <div className="approval-view">
      {reason && <Notice tone={riskTone(request)}>{String(reason)}</Notice>}

      {request.kind === "commandExecution" && (
        <Section title="命令" description="完整命令可能包含本地路径或参数，请确认来源。">
          <CodeBlock>{formatCommand(params.command)}</CodeBlock>
          {params.cwd && (
            <details className="sensitive-details">
              <summary>工作目录</summary>
              <code>{String(params.cwd)}</code>
            </details>
          )}
        </Section>
      )}

      {request.kind === "fileChange" && (
        <Section title="文件改动" description={`${files.length} 个文件`}>
          <DiffViewer files={files} />
        </Section>
      )}

      {request.kind === "permissions" && (
        <Section title="请求的权限">
          <PermissionRows
            permissions={params.permissions}
            answers={answers}
            icon={<ShieldAlert aria-hidden="true" />}
            onAnswer={onAnswer}
          />
        </Section>
      )}

      {request.kind === "network" && (
        <Section title="网络目标">
          <div className="approval-facts">
            <Fact icon={<Globe2 aria-hidden="true" />} label="主机" value={params.host ?? params.hostname ?? "未知"} />
            <Fact label="协议" value={params.protocol ?? "未指定"} />
            <Fact label="端口" value={params.port ?? "默认"} />
            <Fact label="范围" value={params.scope ?? params.access ?? "本次操作"} />
          </div>
        </Section>
      )}

      {(request.kind === "userInput" || request.kind === "elicitation") && params.mode !== "url" && (
        <QuestionForm questions={questions} answers={answers} onChange={onAnswer} />
      )}

      {request.kind === "elicitation" && params.mode === "url" && params.url && (
        <Section title="外部授权">
          <button
            type="button"
            className="widget-button widget-button-secondary"
            onClick={() => onOpenUrl?.(String(params.url))}
          >
            <ExternalLink aria-hidden="true" />打开授权页面
          </button>
        </Section>
      )}

      {!["commandExecution", "fileChange", "permissions", "network", "userInput", "elicitation"].includes(request.kind ?? "") && (
        <Section title="操作详情">
          <div className="approval-facts">
            <Fact icon={<Wrench aria-hidden="true" />} label="类型" value={request.kind ?? request.method ?? "未知"} />
            {params.command && <Fact icon={<Terminal aria-hidden="true" />} label="命令" value={formatCommand(params.command)} />}
          </div>
        </Section>
      )}
    </div>
  );
}

function PermissionRows({
  permissions,
  answers,
  icon,
  onAnswer,
}: {
  permissions: unknown;
  answers: QuestionAnswers;
  icon: React.ReactNode;
  onAnswer(id: string, value: unknown): void;
}) {
  if (!permissions || typeof permissions !== "object") {
    return <Notice tone="warning">宿主没有提供结构化权限详情。</Notice>;
  }
  return (
    <div className="approval-permissions">
      {Object.entries(permissions as Record<string, unknown>).map(([label, value], index) => (
        <label className="approval-permission" key={label}>
          <input
            type="checkbox"
            checked={answers[permissionAnswerKey(label)] !== false}
            onChange={(event) => onAnswer(permissionAnswerKey(label), event.target.checked)}
          />
          {index === 0 && <span className="approval-permission-icon">{icon}</span>}
          <span>
            <strong>{humanize(label)}</strong>
            <small>{formatValue(value)}</small>
          </span>
        </label>
      ))}
    </div>
  );
}

function Fact({ icon, label, value }: { icon?: React.ReactNode; label: string; value: React.ReactNode }) {
  return (
    <div className="approval-fact">
      {icon && <span>{icon}</span>}
      <strong>{label}</strong>
      <span>{value}</span>
    </div>
  );
}

function approvalFiles(diff: unknown, fileChanges: unknown, patch?: unknown): FileDiff[] {
  const text = (typeof diff === "string" && diff)
    ? diff
    : (typeof patch === "string" && patch ? patch : "");
  if (text) return parseAnyDiff(text);
  if (Array.isArray(fileChanges)) {
    return fileChanges.map((path) => ({
      path: String(path), kind: "update", lines: [], adds: 0, dels: 0,
    }));
  }
  if (fileChanges && typeof fileChanges === "object") {
    return Object.entries(fileChanges as Record<string, any>).map(([path, value]) => ({
      path,
      kind: value?.kind ?? "update",
      lines: [],
      adds: 0,
      dels: 0,
    }));
  }
  return [];
}

function schemaOptions(schema: any) {
  if (Array.isArray(schema.enum)) {
    return schema.enum.map((value: unknown, index: number) => ({
      value,
      label: String(schema.enumNames?.[index] ?? value),
    }));
  }
  if (Array.isArray(schema.oneOf)) {
    return schema.oneOf.map((option: any) => ({
      value: option.const,
      label: String(option.title ?? option.const),
    }));
  }
  const items = schema.items ?? {};
  if (Array.isArray(items.enum)) {
    return items.enum.map((value: unknown) => ({ value, label: String(value) }));
  }
  return null;
}

function formatCommand(command: unknown): string {
  return Array.isArray(command) ? command.map(String).join(" ") : String(command ?? "");
}

function formatValue(value: unknown): string {
  if (Array.isArray(value)) return value.join(", ");
  if (value && typeof value === "object") return Object.entries(value as Record<string, unknown>)
    .map(([key, item]) => `${humanize(key)}: ${formatValue(item)}`)
    .join(" · ");
  return String(value ?? "未指定");
}

function humanize(value: string): string {
  return value
    .replace(/[_-]+/g, " ")
    .replace(/([a-z])([A-Z])/g, "$1 $2")
    .replace(/^./, (first) => first.toUpperCase());
}

function permissionAnswerKey(key: string): string {
  return `permission:${key}`;
}

function riskTone(request: ApprovalRequest): "info" | "warning" | "danger" {
  const params = approvalParams(request);
  if (params.risk === "high" || params.destructive === true) return "danger";
  if (request.kind === "commandExecution" || request.kind === "permissions" || request.kind === "network") {
    return "warning";
  }
  return "info";
}

function isEmpty(value: unknown): boolean {
  return value == null || value === "" || (Array.isArray(value) && value.length === 0);
}
