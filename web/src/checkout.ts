import type { AppInfo, Order, OrderItem } from "./types.ts";

export type PurchasableApp = AppInfo & { price_rub: string };

export function canPurchaseApp(app: AppInfo): app is PurchasableApp {
  return app.available && !app.coming_soon && typeof app.price_rub === "string";
}

export function kopecks(amount: string): number {
  if (!/^\d+(?:\.\d{1,2})?$/.test(amount)) {
    throw new Error("Некорректная цена приложения.");
  }
  const [rubles, fraction = ""] = amount.split(".");
  const value = Number(rubles) * 100 + Number(fraction.padEnd(2, "0"));
  if (!Number.isSafeInteger(value))
    throw new Error("Некорректная сумма заказа.");
  return value;
}

export function cartAmount(apps: Pick<AppInfo, "price_rub">[]): string {
  const total = apps.reduce((sum, app) => {
    if (app.price_rub === null)
      throw new Error("Цена приложения ещё не определена.");
    return sum + kopecks(app.price_rub);
  }, 0);
  if (!Number.isSafeInteger(total))
    throw new Error("Некорректная сумма заказа.");
  return `${Math.floor(total / 100)}.${String(total % 100).padStart(2, "0")}`;
}

export function cartRequestKey(slugs: string[], mode = "test"): string {
  return `cart:${mode}:${JSON.stringify([...new Set(slugs)].sort())}`;
}

export function orderItems(order: Order): OrderItem[] {
  return order.items?.length
    ? order.items
    : [
        {
          app_slug: order.app_slug,
          app_title: order.app_title,
          amount_rub: order.amount_rub,
          installed: false,
          activated: false,
          can_install: order.can_install,
          can_activate: false,
          can_request_code: false,
          server_now: "",
          install_expires_at: null,
          activation_expires_at: null,
          access_status: "unavailable",
        },
      ];
}

export function appCount(count: number): string {
  const last = count % 10;
  const teen = count % 100;
  const word =
    teen >= 11 && teen <= 14
      ? "приложений"
      : last === 1
        ? "приложение"
        : last >= 2 && last <= 4
          ? "приложения"
          : "приложений";
  return `${count} ${word}`;
}

export interface PurchasedApp {
  order: Order;
  item: OrderItem;
}

export function purchasedApps(orders: Order[], mode: string): PurchasedApp[] {
  const result: PurchasedApp[] = [];
  const sorted = [...orders].sort((a, b) =>
    b.created_at.localeCompare(a.created_at),
  );
  for (const order of sorted) {
    if (order.status !== "succeeded" || order.test !== (mode === "test"))
      continue;
    for (const item of orderItems(order)) {
      result.push({ order, item });
    }
  }
  return result;
}

export function routeForItem(orderId: string, slug: string): string {
  return `/orders/${encodeURIComponent(orderId)}/apps/${encodeURIComponent(slug)}`;
}
