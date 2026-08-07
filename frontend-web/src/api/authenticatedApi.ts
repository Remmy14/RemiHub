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

  if (response.status === 401 || response.status === 403) {
    const message =
      response.status === 401
        ? "Your RemiHub session expired. Sign in again."
        : "This RemiHub account is not authorized for that request.";
    markAuthenticationExpired(message);
    throw new ApiAuthenticationError(message);
  }

  const data = await response.json().catch(() => null);
  if (!response.ok) {
    const detail =
      typeof data?.detail === "string"
        ? data.detail
        : "RemiHub request failed.";
    throw new Error(detail);
  }

  return data as T;
}
