import assert from "node:assert/strict";
import test from "node:test";
import {
  countdown,
  itemAccess,
  remainingSeconds,
  serverClock,
  serverTime,
  synchronizeClock,
} from "../src/access.ts";
import type { OrderItem } from "../src/types.ts";

const start = "2026-10-15T12:00:00Z";
const installation = "2026-10-16T00:00:00Z";
const activation = "2026-10-15T18:00:00Z";
const item = (overrides: Partial<OrderItem> = {}): OrderItem => ({
  app_slug: "happ",
  app_title: "Happ",
  amount_rub: "490.00",
  installed: false,
  activated: false,
  can_install: true,
  can_activate: false,
  can_request_code: false,
  server_now: start,
  install_expires_at: installation,
  activation_expires_at: null,
  access_status: "install",
  ...overrides,
});

test("deadlines use server time and monotonic elapsed time, independent of client clock", () => {
  const clock = serverClock(start, 500);
  const original = Date.now;
  try {
    Date.now = () => Date.parse("2040-01-01T00:00:00Z");
    assert.equal(
      remainingSeconds(installation, serverTime(clock, 1500)),
      12 * 3600 - 1,
    );
    Date.now = () => Date.parse("2000-01-01T00:00:00Z");
    assert.equal(
      remainingSeconds(installation, serverTime(clock, 2500)),
      12 * 3600 - 2,
    );
  } finally {
    Date.now = original;
  }
  const older = synchronizeClock(clock, start, 6500);
  assert.equal(
    remainingSeconds(installation, serverTime(older, 6500)),
    12 * 3600 - 6,
  );
  assert.equal(
    remainingSeconds(installation, serverTime(older, 7500)),
    12 * 3600 - 7,
  );
});

test("installation closes at the exact 12-hour deadline and never reopens after installed", () => {
  assert.equal(itemAccess(item(), Date.parse(installation) - 1), "install");
  assert.equal(itemAccess(item(), Date.parse(installation)), "install_expired");
  assert.equal(
    itemAccess(item({ installed: true }), Date.parse(start)),
    "unavailable",
  );
  assert.equal(
    itemAccess(item({ can_install: false }), Date.parse(start)),
    "unavailable",
  );
});

test("activation has its own six-hour deadline and completed stays completed without delivery rights", () => {
  const installed = item({
    installed: true,
    can_install: false,
    can_activate: true,
    can_request_code: true,
    activation_expires_at: activation,
    access_status: "activate",
  });
  assert.equal(itemAccess(installed, Date.parse(activation) - 1), "activate");
  assert.equal(
    itemAccess(installed, Date.parse(activation)),
    "activation_expired",
  );
  assert.equal(
    itemAccess(
      { ...installed, activated: true, can_activate: false },
      Date.parse(installation) + 1,
    ),
    "completed",
  );
  assert.equal(
    itemAccess({ ...installed, can_activate: false }, Date.parse(start)),
    "unavailable",
  );
});

test("unknown dates fail closed and the code-request cooldown is separate from the activation window", () => {
  assert.equal(
    itemAccess(item(), serverTime(serverClock("invalid", 0), 1)),
    "install_expired",
  );
  assert.equal(remainingSeconds(null, Date.parse(start)), 0);
  assert.equal(remainingSeconds("invalid", Date.parse(start)), 0);
  const retryAt = "2026-10-15T12:01:00Z";
  assert.equal(remainingSeconds(retryAt, Date.parse(start)), 60);
  assert.equal(
    remainingSeconds(activation, Date.parse(retryAt)),
    6 * 3600 - 60,
  );
  assert.equal(
    countdown(remainingSeconds(activation, Date.parse(start))),
    "06:00:00",
  );
});
