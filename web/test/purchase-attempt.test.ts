import assert from "node:assert/strict";
import test from "node:test";
import {
  bindPurchaseRequest,
  clearPurchaseRequest,
  purchaseRequestId,
} from "../src/api.ts";
import { cartRequestKey } from "../src/checkout.ts";
import { settlePurchaseOrders } from "../src/purchase.ts";
import type { Order } from "../src/types.ts";

const values = new Map<string, string>();
Object.defineProperty(globalThis, "sessionStorage", {
  configurable: true,
  value: {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => values.set(key, value),
    removeItem: (key: string) => values.delete(key),
  },
});

test("an old order cannot clear the request of a repeat purchase with the same app", () => {
  const key = "cart:live:repeat";
  const first = purchaseRequestId(key);
  bindPurchaseRequest(key, "order-old", first);
  assert.equal(clearPurchaseRequest(key, "order-old"), true);
  const second = purchaseRequestId(key);
  assert.notEqual(first, second);
  bindPurchaseRequest(key, "order-new", second);
  assert.equal(clearPurchaseRequest(key, "order-old"), false);
  assert.equal(purchaseRequestId(key), second);
  assert.equal(clearPurchaseRequest(key, "order-new"), true);
  assert.equal(clearPurchaseRequest(key, "order-new"), false);
});

test("a legacy unbound request resolves once, binds to its POST result and then clears", () => {
  const key = "cart:live:legacy";
  values.set(`appki.purchase.${key}`, "legacy-request-id");
  assert.equal(purchaseRequestId(key), "legacy-request-id");
  assert.equal(clearPurchaseRequest(key, "unrelated-old-order"), false);
  bindPurchaseRequest(key, "resolved-order", "legacy-request-id");
  assert.equal(clearPurchaseRequest(key, "resolved-order"), true);
  const fresh = purchaseRequestId(key);
  assert.notEqual(fresh, "legacy-request-id");
  bindPurchaseRequest(key, "new-order", fresh);
  assert.equal(clearPurchaseRequest(key, "resolved-order"), false);
  assert.equal(purchaseRequestId(key), fresh);
});

test("a late POST cannot rebind a new attempt after the previous terminal response", () => {
  const key = "cart:live:late-response";
  const oldRequest = purchaseRequestId(key);
  assert.equal(bindPurchaseRequest(key, "old-order", oldRequest), true);
  assert.equal(clearPurchaseRequest(key, "old-order"), true);
  const newRequest = purchaseRequestId(key);
  assert.equal(bindPurchaseRequest(key, "new-order", newRequest), true);
  assert.equal(bindPurchaseRequest(key, "old-order", oldRequest), false);
  assert.equal(clearPurchaseRequest(key, "old-order"), false);
  assert.equal(purchaseRequestId(key), newRequest);
  assert.equal(clearPurchaseRequest(key, "new-order"), true);
  assert.equal(bindPurchaseRequest(key, "new-order", newRequest), false);
});

function snapshot(
  id: string,
  slug: string,
  status: Order["status"] = "succeeded",
): Order {
  return {
    id,
    app_slug: slug,
    app_title: slug,
    amount_rub: "490.00",
    currency: "RUB",
    status,
    created_at: "2026-10-15T12:00:00Z",
    test: false,
    can_install: status === "succeeded",
    has_payment: true,
    can_retry_payment: false,
    requires_payment_review: false,
  };
}

test("a reopened paid snapshot settles its persisted attempt before another checkout", () => {
  const slug = "reopened-app";
  const key = cartRequestKey([slug], "live");
  values.set(`appki.purchase.${key}`, "persisted-before-webhook");
  values.set(`appki.purchase.${key}.order`, "webhook-paid-order");
  assert.deepEqual(
    settlePurchaseOrders([snapshot("webhook-paid-order", slug)]),
    [
      {
        orderId: "webhook-paid-order",
        status: "succeeded",
        purchasedSlugs: [slug],
      },
    ],
  );
  assert.equal(values.has(`appki.purchase.${key}`), false);
  assert.notEqual(purchaseRequestId(key), "persisted-before-webhook");
  assert.deepEqual(
    settlePurchaseOrders([snapshot("webhook-paid-order", slug)]),
    [],
  );
});

test("refreshing an older paid order preserves a newer bound attempt for the same cart", () => {
  const slug = "newer-attempt-app";
  const key = cartRequestKey([slug], "live");
  const requestId = purchaseRequestId(key);
  bindPurchaseRequest(key, "newer-order", requestId);
  assert.deepEqual(
    settlePurchaseOrders([snapshot("older-paid-order", slug)]),
    [],
  );
  assert.equal(purchaseRequestId(key), requestId);
  assert.deepEqual(settlePurchaseOrders([snapshot("newer-order", slug)]), [
    {
      orderId: "newer-order",
      status: "succeeded",
      purchasedSlugs: [slug],
    },
  ]);
});

test("canceled and refunded attempts settle without purchased slugs or success effects", () => {
  for (const status of ["canceled", "refunded"] as const) {
    const slug = `${status}-app`;
    const key = cartRequestKey([slug], "live");
    const requestId = purchaseRequestId(key);
    bindPurchaseRequest(key, `${status}-order`, requestId);
    assert.deepEqual(
      settlePurchaseOrders([snapshot(`${status}-order`, slug, status)]),
      [
        {
          orderId: `${status}-order`,
          status,
          purchasedSlugs: [],
        },
      ],
    );
    assert.notEqual(purchaseRequestId(key), requestId);
  }
});

test("partial refresh failures and pending snapshots never infer a terminal attempt", async () => {
  const slug = "partial-refresh-app";
  const key = cartRequestKey([slug], "live");
  const requestId = purchaseRequestId(key);
  bindPurchaseRequest(key, "pending-order", requestId);
  const responses = await Promise.allSettled([
    Promise.reject(new Error("paid list unavailable")),
    Promise.resolve({ items: [snapshot("pending-order", slug, "pending")] }),
  ]);
  const incoming = responses.flatMap((response) =>
    response.status === "fulfilled" ? response.value.items : [],
  );
  assert.deepEqual(settlePurchaseOrders(incoming), []);
  assert.deepEqual(settlePurchaseOrders([]), []);
  assert.equal(purchaseRequestId(key), requestId);
});
