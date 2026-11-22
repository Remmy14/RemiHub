import {
  getFirebaseIdToken,
  markAuthenticationExpired,
} from "../auth/firebaseAuth";

export class ApiAuthenticationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ApiAuthenticationError";
  }
}

export class ApiForbiddenError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ApiForbiddenError";
  }
}

export class ApiCooldownError extends Error {
  readonly retryAt: string | null;

  constructor(retryAt: string | null) {
    super("Please wait before starting another verification.");
    this.name = "ApiCooldownError";
    this.retryAt = retryAt;
  }
}

export class ApiHttpError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiHttpError";
    this.status = status;
  }
}

type ApiRequestOptions = RequestInit & {
  authenticated?: boolean;
};

export async function apiRequest<T>(
  path: string,
  options: ApiRequestOptions = {},
): Promise<T> {
  const headers = new Headers(options.headers);

  if (options.authenticated !== false) {
    const idToken = await getFirebaseIdToken();
    headers.set("Authorization", `Bearer ${idToken}`);
  }

  if (options.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  const response = await fetch(path, {
    ...options,
    headers,
  });

  if (response.status === 401) {
    const message = "Your RemiHub session expired. Sign in again.";
    markAuthenticationExpired(message);
    throw new ApiAuthenticationError(message);
  }

  if (response.status === 403) {
    throw new ApiForbiddenError(
      "This RemiHub account is not authorized for that request.",
    );
  }

  const data = await response.json().catch(() => null);
  if (!response.ok) {
    if (response.status === 429 && data?.detail?.code === "FINANCE_VERIFICATION_COOLDOWN") {
      throw new ApiCooldownError(typeof data.detail.retry_at === "string" ? data.detail.retry_at : null);
    }
    const detail =
      typeof data?.detail === "string"
        ? data.detail
        : Array.isArray(data?.detail)
          ? (data.detail as Array<{ msg?: string }>)
              .map((entry) => entry?.msg)
              .filter((message): message is string => typeof message === "string")
              .join("; ") || "Check the supplied values."
        : "RemiHub request failed.";
    throw new ApiHttpError(detail, response.status);
  }

  return data as T;
}
