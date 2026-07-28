import React from "react";

export interface Choice<T extends string> {
  value: T;
  title: React.ReactNode;
  description?: React.ReactNode;
  disabled?: boolean;
  icon?: React.ReactNode;
}

export function ChoiceList<T extends string>({
  label,
  value,
  choices,
  onChange,
}: {
  label: string;
  value: T;
  choices: Array<Choice<T>>;
  onChange(value: T): void;
}) {
  return (
    <div className="widget-choice-list" role="radiogroup" aria-label={label}>
      {choices.map((choice) => (
        <button
          type="button"
          role="radio"
          aria-checked={value === choice.value}
          className="widget-choice"
          data-active={value === choice.value}
          disabled={choice.disabled}
          key={choice.value}
          onClick={() => onChange(choice.value)}
        >
          <span className="widget-radio" aria-hidden="true" />
          {choice.icon}
          <span className="min-w-0">
            <span className="widget-choice-title">{choice.title}</span>
            {choice.description && (
              <span className="widget-choice-description">{choice.description}</span>
            )}
          </span>
        </button>
      ))}
    </div>
  );
}
