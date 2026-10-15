import type { User } from "./types";
const SESSION_KEY = "appki.session.v1";
let token: string | null = null;
const purchaseAttempts = new Map<
  string,
  { id: string; orderId: string | null }
>();
export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}
export function readSession(): boolean {
  try {
    const value = JSON.parse(sessionStorage.getItem(SESSION_KEY) ?? "null");
    if (
      value &&
      typeof value.token === "string" &&
      Date.parse(value.expires_at) > Date.now()
    ) {
      token = value.token;
      return true;
    }
  } catch {
    /* Storage may be disabled in a webview. */
  }
  clearSession();
  return false;
}
export function setSession(accessToken: string, expiresAt: string) {
  token = accessToken;
  try {
    sessionStorage.setItem(
      SESSION_KEY,
      JSON.stringify({ token, expires_at: expiresAt }),
    );
  } catch {
    /* The in-memory session remains usable. */
  }
}
export function clearSession() {
  token = null;
  try {
    sessionStorage.removeItem(SESSION_KEY);
  } catch {
    /* No storage available. */
  }
}
export async function api<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const headers = new Headers(options.headers);
  if (options.body) headers.set("Content-Type", "application/json");
  if (token) headers.set("Authorization", `Bearer ${token}`);
  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      ...options,
      headers,
      credentials: "omit",
      cache: "no-store",
    });
  } catch {
    throw new ApiError(
      "Не удалось связаться с сервером. Проверьте интернет и попробуйте ещё раз.",
      0,
    );
  }
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    if (response.status === 401) {
      clearSession();
      window.dispatchEvent(new Event("appki:unauthorized"));
    }
    throw new ApiError(
      typeof data?.detail === "string"
        ? data.detail
        : "Не удалось выполнить действие. Попробуйте ещё раз.",
      response.status,
    );
  }
  return data as T;
}
export async function authenticate(): Promise<User | null> {
  const initData = window.Telegram?.WebApp?.initData;
  if (initData) {
    const result = await api<{
      access_token: string;
      expires_at: string;
      user: User;
    }>("/auth/telegram", {
      method: "POST",
      body: JSON.stringify({ init_data: initData }),
    });
    setSession(result.access_token, result.expires_at);
    return result.user;
  }
  if (readSession()) return (await api<{ user: User }>("/me")).user;
  return null;
}
export function purchaseRequestId(slug: string): string {
  const key = `appki.purchase.${slug}`;
  const cached = purchaseAttempts.get(slug);
  if (cached) return cached.id;
  try {
    const existing = sessionStorage.getItem(key);
    if (existing) {
      purchaseAttempts.set(slug, {
        id: existing,
        orderId: sessionStorage.getItem(`${key}.order`),
      });
      return existing;
    }
  } catch {
    /* Retry in this page is also held in React state. */
  }
  const id = crypto.randomUUID();
  purchaseAttempts.set(slug, { id, orderId: null });
  try {
    sessionStorage.setItem(key, id);
  } catch {
    /* Optional persistence. */
  }
  return id;
}
export function bindPurchaseRequest(
  slug: string,
  orderId: string,
  expectedId: string,
): boolean {
  let id = purchaseAttempts.get(slug)?.id;
  try {
    id ??= sessionStorage.getItem(`appki.purchase.${slug}`) ?? undefined;
  } catch {
    /* The in-memory attempt is sufficient when storage is unavailable. */
  }
  if (id !== expectedId) return false;
  purchaseAttempts.set(slug, { id, orderId });
  try {
    sessionStorage.setItem(`appki.purchase.${slug}.order`, orderId);
  } catch {
    /* The current page retains the attempt when storage is unavailable. */
  }
  return true;
}
export function clearPurchaseRequest(slug: string, orderId?: string): boolean {
  let boundOrder = purchaseAttempts.get(slug)?.orderId;
  try {
    boundOrder ??= sessionStorage.getItem(`appki.purchase.${slug}.order`);
  } catch {
    /* Optional persistence. */
  }
  if (orderId !== undefined && boundOrder !== orderId) return false;
  purchaseAttempts.delete(slug);
  try {
    sessionStorage.removeItem(`appki.purchase.${slug}`);
    sessionStorage.removeItem(`appki.purchase.${slug}.order`);
  } catch {
    /* Optional persistence. */
  }
  return true;
}
