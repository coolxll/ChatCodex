import React from "react";

export interface Segment<T extends string> {
  value: T;
  label: React.ReactNode;
  disabled?: boolean;
}

export function SegmentedControl<T extends string>({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: T;
  options: Array<Segment<T>>;
  onChange(value: T): void;
}) {
  return (
    <div
      className="widget-segmented"
      role="radiogroup"
      aria-label={label}
      style={{ gridTemplateColumns: `repeat(${options.length}, minmax(0, 1fr))` }}
    >
      {options.map((option) => (
        <button
          type="button"
          role="radio"
          aria-checked={value === option.value}
          className="widget-segment"
          data-active={value === option.value}
          disabled={option.disabled}
          key={option.value}
          onClick={() => onChange(option.value)}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}
