export type PrivateApiFailureReason =
  | "unavailable"
  | "disabled"
  | "user_activation_required"
  | "cancelled"
  | "invalid_result"
  | "rejected";

export type PrivateApiResult<T = void> =
  | { ok: true; value: T }
  | { ok: false; reason: PrivateApiFailureReason; error?: Error };

export function privateApiFailure(
  reason: PrivateApiFailureReason,
  cause?: unknown,
): PrivateApiResult<never> {
  const error = cause instanceof Error
    ? cause
    : cause == null
      ? undefined
      : new Error(String(cause));
  return { ok: false, reason, error };
}
