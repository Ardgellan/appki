import assert from "node:assert/strict";
import test from "node:test";
import {
  appCount,
  cartAmount,
  cartRequestKey,
  kopecks,
  orderItems,
  purchasedApps,
  routeForItem,
} from "../src/checkout.ts";
import type { Order } from "../src/types.ts";

const order = (overrides: Partial<Order> = {}): Order => ({
  id: "order-1",
  app_slug: "happ",
  app_title: "Happ",
  amount_rub: "790.00",
  currency: "RUB",
  status: "succeeded",
  has_payment: true,
  can_retry_payment: false,
  requires_payment_review: false,
  created_at: "2026-10-13T12:00:00Z",
  test: true,
  can_install: true,
  ...overrides,
});

test("cart sums money exactly, including fractional rubles", () => {
  assert.equal(
    cartAmount([{ price_rub: "0.10" }, { price_rub: "0.20" }]),
    "0.30",
  );
  assert.equal(
    cartAmount([{ price_rub: "790.00" }, { price_rub: "790.00" }]),
    "1580.00",
  );
  assert.equal(cartAmount([]), "0.00");
  for (const value of ["NaN", "-1", "1.001", "1e3", "Infinity"])
    assert.throws(() => kopecks(value));
});

test("purchase retries have a stable key regardless of selection order", () => {
  assert.notEqual(
    cartRequestKey(["happ"], "test"),
    cartRequestKey(["happ"], "live"),
  );
  assert.equal(
    cartRequestKey(["happ", "v2ray"]),
    cartRequestKey(["v2ray", "happ", "happ"]),
  );
  assert.notEqual(cartRequestKey(["happ"]), cartRequestKey(["happ", "v2ray"]));
});

test("legacy orders retain installation access without inventing activation", () => {
  assert.deepEqual(orderItems(order())[0], {
    app_slug: "happ",
    app_title: "Happ",
    amount_rub: "790.00",
    installed: false,
    activated: false,
    can_install: true,
  });
});

test("my apps only includes verified paid orders in the current payment mode", () => {
  const items = [
    {
      app_slug: "happ",
      app_title: "Happ",
      amount_rub: "790.00",
      installed: true,
      activated: true,
      can_install: true,
    },
    {
      app_slug: "v2ray",
      app_title: "V2RayTun",
      amount_rub: "790.00",
      installed: false,
      activated: false,
      can_install: true,
    },
  ];
  const old = order({ id: "old", created_at: "2026-10-12T12:00:00Z" });
  const newer = order({ id: "new", items });
  const pending = order({ id: "pending", status: "pending" });
  const refunded = order({ id: "refunded", status: "refunded" });
  const live = order({ id: "live", test: false });
  const owned = purchasedApps([old, pending, newer, refunded, live], "test");
  assert.equal(owned.length, 2);
  assert.equal(owned[0].order.id, "new");
  assert.equal(owned[0].item.activated, true);
  assert.equal(
    purchasedApps([old, pending, newer, refunded, live], "live")[0].order.id,
    "live",
  );
});

test("item routes encode identifiers and app counts handle teens", () => {
  assert.equal(
    routeForItem("id/1", "app name"),
    "/orders/id%2F1/apps/app%20name",
  );
  assert.equal(appCount(1), "1 приложение");
  assert.equal(appCount(2), "2 приложения");
  assert.equal(appCount(11), "11 приложений");
  assert.equal(appCount(21), "21 приложение");
});
