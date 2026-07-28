export interface QuestionOption {
  label: string;
  value?: unknown;
  description?: string;
}

export interface QuestionField {
  id: string;
  header?: string;
  question: string;
  required?: boolean;
  isOther?: boolean;
  isSecret?: boolean;
  multiple?: boolean;
  type?: "string" | "number" | "integer" | "boolean" | "array";
  options?: QuestionOption[] | null;
  minimum?: number;
  maximum?: number;
  minLength?: number;
  maxLength?: number;
}

export type QuestionAnswers = Record<string, unknown>;

export function isQuestionAnswered(question: QuestionField, value: unknown): boolean {
  const empty = value === undefined ||
    value === null ||
    value === "" ||
    (Array.isArray(value) && value.length === 0);
  if (empty) return !question.required;
  if (question.multiple || question.type === "array") {
    return Array.isArray(value) && value.length > 0;
  }
  if (question.type === "boolean") return typeof value === "boolean";
  if (question.type === "number" || question.type === "integer") {
    if (typeof value !== "number" || !Number.isFinite(value)) return false;
    if (question.type === "integer" && !Number.isInteger(value)) return false;
    if (question.minimum != null && value < question.minimum) return false;
    if (question.maximum != null && value > question.maximum) return false;
    return true;
  }
  if (typeof value !== "string") return true;
  if (question.minLength != null && value.length < question.minLength) return false;
  if (question.maxLength != null && value.length > question.maxLength) return false;
  return true;
}

export function QuestionForm({
  questions,
  answers,
  onChange,
}: {
  questions: QuestionField[];
  answers: QuestionAnswers;
  onChange(id: string, value: unknown): void;
}) {
  return (
    <div className="question-list">
      {questions.map((question, index) => {
        const value = answers[question.id];
        const options = question.type === "boolean"
          ? [{ label: "是", value: true }, { label: "否", value: false }]
          : question.options ?? [];
        const optionValues = options.map((option) => option.value ?? option.label);
        const otherValue = question.multiple
          ? (Array.isArray(value)
              ? value.find((item) => !optionValues.some((option) => Object.is(option, item)))
              : undefined)
          : optionValues.some((option) => Object.is(option, value))
            ? undefined
            : value;
        return (
          <section
            className="question-field"
            key={question.id}
            aria-labelledby={`question-${question.id}`}
          >
            <div className="question-heading">
              <span className="question-index" aria-hidden="true">{index + 1}</span>
              <div>
                {question.header && <div className="question-header">{question.header}</div>}
                <h2 id={`question-${question.id}`} className="question-title">
                  {question.question}
                  {question.required && <span aria-label="必填"> *</span>}
                </h2>
              </div>
            </div>
            {options.length ? (
              <div
                className="widget-choice-list question-options"
                role={question.multiple ? "group" : "radiogroup"}
                aria-labelledby={`question-${question.id}`}
              >
                {options.map((option) => {
                  const optionValue = option.value ?? option.label;
                  const selected = question.multiple
                    ? Array.isArray(value) && value.includes(optionValue)
                    : value === optionValue;
                  return (
                    <button
                      type="button"
                      role={question.multiple ? "checkbox" : "radio"}
                      aria-checked={selected}
                      className="widget-choice"
                      data-active={selected}
                      key={`${question.id}-${String(optionValue)}`}
                      onClick={() => {
                        if (!question.multiple) {
                          onChange(question.id, optionValue);
                          return;
                        }
                        const current = Array.isArray(value) ? value : [];
                        onChange(
                          question.id,
                          selected
                            ? current.filter((item) => item !== optionValue)
                            : [...current, optionValue],
                        );
                      }}
                    >
                      <span className={question.multiple ? "widget-checkbox" : "widget-radio"} aria-hidden="true" />
                      <span className="min-w-0">
                        <span className="widget-choice-title">{option.label}</span>
                        {option.description && (
                          <span className="widget-choice-description">{option.description}</span>
                        )}
                      </span>
                    </button>
                  );
                })}
                {question.isOther && (
                  <label className="question-other">
                    <span>其他</span>
                    <input
                      type={question.isSecret ? "password" : "text"}
                      aria-label={`${question.question}的其他答案`}
                      value={typeof otherValue === "string" ? otherValue : ""}
                      onChange={(event) => {
                        const next = event.target.value;
                        if (!question.multiple) {
                          onChange(question.id, next);
                          return;
                        }
                        const current = Array.isArray(value) ? value : [];
                        const selectedOptions = current.filter((item) =>
                          optionValues.some((option) => Object.is(option, item)));
                        onChange(
                          question.id,
                          next ? [...selectedOptions, next] : selectedOptions,
                        );
                      }}
                    />
                  </label>
                )}
              </div>
            ) : (
              <input
                className="widget-input"
                aria-labelledby={`question-${question.id}`}
                type={question.isSecret
                  ? "password"
                  : question.type === "number" || question.type === "integer"
                    ? "number"
                    : "text"}
                min={question.minimum}
                max={question.maximum}
                minLength={question.minLength}
                maxLength={question.maxLength}
                value={typeof value === "number" || typeof value === "string" ? value : ""}
                onChange={(event) => {
                  const next = question.type === "number" || question.type === "integer"
                    ? event.target.value === "" ? "" : Number(event.target.value)
                    : event.target.value;
                  onChange(question.id, next);
                }}
              />
            )}
          </section>
        );
      })}
    </div>
  );
}
