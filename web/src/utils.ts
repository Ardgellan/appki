export function rubles(amount: string): string {
  const value = Number(amount);
  return Number.isFinite(value)
    ? new Intl.NumberFormat("ru-RU", {
        style: "currency",
        currency: "RUB",
        maximumFractionDigits: value % 1 === 0 ? 0 : 2,
      }).format(value)
    : "—";
}
export function fileSize(bytes: number): string {
  return `${Math.round(bytes / 1_000_000)} МБ`;
}
export function httpsURL(value: string | undefined): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    return url.protocol === "https:" && !url.username && !url.password
      ? url.href
      : null;
  } catch {
    return null;
  }
}
export function telegramURL(username: string | undefined): string | null {
  const clean = username?.replace(/^@/, "") ?? "";
  return /^[a-zA-Z0-9_]{5,32}$/.test(clean) ? `https://t.me/${clean}` : null;
}
export function orderStatus(status: string): string {
  return (
    {
      pending: "Ожидает оплаты",
      succeeded: "Оплачено",
      canceled: "Оплата отменена",
      refunded: "Возврат оформлен",
    }[status] ?? "Проверяем статус"
  );
}
export function routeForOrder(id: string): string {
  return `/orders/${encodeURIComponent(id)}`;
}
export function formatDate(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? ""
    : date.toLocaleDateString("ru-RU", { day: "numeric", month: "long" });
}
export function formatTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? ""
    : date.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
}
