import type { AccessStatus, OrderItem } from "./types.ts";

export interface ServerClock {
  serverTime: number;
  receivedAt: number;
}

export function serverClock(
  serverNow: string,
  receivedAt: number,
): ServerClock {
  return { serverTime: Date.parse(serverNow), receivedAt };
}

export function serverTime(clock: ServerClock, monotonicNow: number): number {
  if (!Number.isFinite(clock.serverTime)) return Number.POSITIVE_INFINITY;
  return clock.serverTime + Math.max(0, monotonicNow - clock.receivedAt);
}

export function synchronizeClock(
  previous: ServerClock,
  serverNow: string,
  receivedAt: number,
): ServerClock {
  const fresh = serverClock(serverNow, receivedAt);
  const current = serverTime(previous, receivedAt);
  return Number.isFinite(fresh.serverTime) && Number.isFinite(current)
    ? { ...fresh, serverTime: Math.max(fresh.serverTime, current) }
    : fresh;
}

export function remainingSeconds(deadline: string | null, now: number): number {
  const end = deadline ? Date.parse(deadline) : Number.NaN;
  return Number.isFinite(end) && Number.isFinite(now)
    ? Math.max(0, Math.ceil((end - now) / 1000))
    : 0;
}

export function countdown(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  return [Math.floor(total / 3600), Math.floor((total % 3600) / 60), total % 60]
    .map((part) => String(part).padStart(2, "0"))
    .join(":");
}

export function itemAccess(item: OrderItem, now: number): AccessStatus {
  if (item.activated || item.access_status === "completed") return "completed";
  if (item.access_status === "install") {
    if (!remainingSeconds(item.install_expires_at, now))
      return "install_expired";
    return item.can_install && !item.installed ? "install" : "unavailable";
  }
  if (item.access_status === "activate") {
    if (!remainingSeconds(item.activation_expires_at, now))
      return "activation_expired";
    return item.can_activate && item.installed ? "activate" : "unavailable";
  }
  return item.access_status;
}

export function accessLabel(status: AccessStatus): string {
  return {
    install: "Установить приложение",
    activate: "Нужна активация",
    completed: "Готово",
    install_expired: "Время на установку истекло",
    activation_expired: "Время на активацию истекло",
    unavailable: "Доступ временно недоступен",
  }[status];
}
