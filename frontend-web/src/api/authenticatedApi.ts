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
    const detail =
      typeof data?.detail === "string"
        ? data.detail
        : Array.isArray(data?.detail)
          ? (data.detail as Array<{ msg?: string }>)
              .map((entry) => entry?.msg)
              .filter((message): message is string => typeof message === "string")
              .join("; ") || "Check the supplied values."
        : "RemiHub request failed.";
    throw new Error(detail);
  }

  return data as T;
}
