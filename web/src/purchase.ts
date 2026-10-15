import { clearPurchaseRequest } from "./api.ts";
import { cartRequestKey, orderItems } from "./checkout.ts";
import type { Order } from "./types.ts";

export interface PurchaseSettlement {
  orderId: string;
  status: Order["status"];
  purchasedSlugs: string[];
}

// Only a confirmed terminal order may consume its own saved attempt. An absent
// order or a newer attempt with the same cart is never inferred to be finished.
export function settlePurchaseOrders(orders: Order[]): PurchaseSettlement[] {
  const settled: PurchaseSettlement[] = [];
  for (const order of orders) {
    if (order.status === "pending") continue;
    const slugs = orderItems(order).map((item) => item.app_slug);
    if (
      !clearPurchaseRequest(
        cartRequestKey(slugs, order.test ? "test" : "live"),
        order.id,
      )
    )
      continue;
    settled.push({
      orderId: order.id,
      status: order.status,
      purchasedSlugs: order.status === "succeeded" ? slugs : [],
    });
  }
  return settled;
}
