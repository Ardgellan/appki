import test from "node:test";
import assert from "node:assert/strict";
import { httpsURL, telegramURL, routeForOrder } from "../src/utils.ts";
test("external navigation refuses script, plaintext and userinfo URLs", () => {
  for (const value of [
    "javascript:alert(1)",
    "http://yookassa.ru",
    "https://user:pass@example.com",
    "//example.com",
    "",
    "file:///tmp/test",
  ])
    assert.equal(httpsURL(value), null);
  assert.equal(
    httpsURL("https://yoomoney.ru/checkout/payments/v2/contract?orderId=abc"),
    "https://yoomoney.ru/checkout/payments/v2/contract?orderId=abc",
  );
});
test("Telegram deep links cannot escape the configured bot name", () => {
  assert.equal(telegramURL("@appki_test_bot"), "https://t.me/appki_test_bot");
  for (const value of [
    "foo?start=malicious",
    "appki/test",
    "https://example.com",
    "foo#bar",
    "",
  ])
    assert.equal(telegramURL(value), null);
});
test("order routes encode identifiers rather than treating them as navigation input", () => {
  assert.equal(
    routeForOrder("../payment/return?success=true"),
    "/orders/..%2Fpayment%2Freturn%3Fsuccess%3Dtrue",
  );
});
