import assert from "node:assert/strict";
import test from "node:test";
import {
  bindPurchaseRequest,
  clearPurchaseRequest,
  purchaseRequestId,
} from "../src/api.ts";

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
