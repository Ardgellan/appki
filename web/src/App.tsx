import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
} from "react";
import type { ReactNode } from "react";
import {
  ApiError,
  api,
  authenticate,
  bindPurchaseRequest,
  purchaseRequestId,
} from "./api";
import { settlePurchaseOrders } from "./purchase";
import {
  accessLabel,
  countdown,
  itemAccess,
  remainingSeconds,
  serverClock,
  serverTime,
  synchronizeClock,
} from "./access";
import {
  appCount,
  canPurchaseApp,
  cartAmount,
  cartRequestKey,
  orderItems,
  purchasedApps,
  routeForItem,
} from "./checkout";
import type { PurchasableApp } from "./checkout";
import { AppIcon, BrandMark, Icon } from "./icons";
import type {
  Activation,
  ActivationRequestState,
  AppInfo,
  Config,
  Grant,
  Order,
  OrderItem,
  User,
} from "./types";
import {
  fileSize,
  formatDate,
  formatTime,
  httpsURL,
  orderStatus,
  rubles,
  supportURL,
  telegramURL,
} from "./utils";

interface AppState {
  config: Config | null;
  apps: AppInfo[];
  user: User | null;
  authReady: boolean;
  authError: string;
  path: string;
  navigate: (path: string) => void;
  toast: (message: string, tone?: "default" | "success") => void;
  orders: Order[];
  ordersLoading: boolean;
  ordersReady: boolean;
  ordersError: string;
  refreshOrders: () => Promise<void>;
  recordOrder: (order: Order) => void;
  cart: string[];
  cartReady: boolean;
  toggleCart: (slug: string) => void;
  checkout: (anchor?: HTMLElement) => void;
}
const Context = createContext<AppState>(null!);
const useApp = () => useContext(Context);
const message = (error: unknown) =>
  error instanceof Error ? error.message : "Не получилось. Попробуйте ещё раз.";

function useServerNow(serverNow: string): number {
  const clock = useRef(serverClock(serverNow, performance.now()));
  const lastSource = useRef(serverNow);
  if (lastSource.current !== serverNow) {
    clock.current = synchronizeClock(
      clock.current,
      serverNow,
      performance.now(),
    );
    lastSource.current = serverNow;
  }
  const [tick, setTick] = useState(() => performance.now());
  useEffect(() => {
    const update = () => setTick(performance.now());
    const timer = window.setInterval(update, 1000);
    window.addEventListener("focus", update);
    document.addEventListener("visibilitychange", update);
    return () => {
      clearInterval(timer);
      window.removeEventListener("focus", update);
      document.removeEventListener("visibilitychange", update);
    };
  }, []);
  return serverTime(clock.current, tick);
}

function AccessDeadline({
  deadline,
  now,
  activation = false,
}: {
  deadline: string | null;
  now: number;
  activation?: boolean;
}) {
  return (
    <p className="access-deadline">
      {activation ? "На активацию осталось" : "На установку осталось"}
      <strong>{countdown(remainingSeconds(deadline, now))}</strong>
    </p>
  );
}

function external(value: string) {
  const url = httpsURL(value);
  if (!url) return;
  const tg = window.Telegram?.WebApp;
  if (tg?.initData) tg.openLink(url, { try_instant_view: false });
  else window.open(url, "_blank", "noopener,noreferrer");
}

function Button({
  children,
  onClick,
  disabled,
  secondary = false,
  busy = false,
  className = "",
}: {
  children: ReactNode;
  onClick?: () => void;
  disabled?: boolean;
  secondary?: boolean;
  busy?: boolean;
  className?: string;
}) {
  return (
    <button
      type="button"
      className={`button ${secondary ? "secondary" : ""} ${className}`}
      disabled={disabled || busy}
      onClick={onClick}
      aria-busy={busy || undefined}
    >
      {busy && <span className="spinner" />}
      {children}
    </button>
  );
}

function Notice({
  children,
  tone = "info",
}: {
  children: ReactNode;
  tone?: "info" | "error" | "success";
}) {
  return (
    <div
      className={`notice ${tone}`}
      role={
        tone === "error" ? "alert" : tone === "success" ? "status" : undefined
      }
    >
      <Icon name={tone === "success" ? "check" : "help"} size={20} />
      <div>{children}</div>
    </div>
  );
}
function Skeleton() {
  return (
    <div className="skeleton-wrap" role="status" aria-label="Загрузка">
      <div className="skeleton line" />
      <div className="skeleton block" />
      <div className="skeleton block" />
    </div>
  );
}
function Back({
  label = "Каталог",
  to = "/",
}: {
  label?: string;
  to?: string;
}) {
  const { navigate } = useApp();
  return (
    <button type="button" className="back" onClick={() => navigate(to)}>
      <Icon name="back" size={18} />
      {label}
    </button>
  );
}
function TelegramGate() {
  const { config, authReady, authError } = useApp();
  const link = telegramURL(config?.bot_username);
  if (!authReady) return <Notice>Подключаем ваш профиль Telegram…</Notice>;
  return (
    <div className="telegram-gate">
      <Icon name="telegram" size={24} />
      <div>
        <strong>Продолжите в Telegram</strong>
        <p>Там сохранятся покупки и доступ к установке.</p>
        {authError && <p className="error-text">{authError}</p>}
      </div>
      {link ? (
        <a className="button" href={link} rel="noreferrer">
          Открыть бота <Icon name="arrow" size={18} />
        </a>
      ) : (
        <p className="muted small-text">Бот ещё настраивается.</p>
      )}
    </div>
  );
}
function Header() {
  const { config, navigate } = useApp();
  return (
    <header className="header">
      <button
        type="button"
        className="brand"
        onClick={() => navigate("/")}
        aria-label="Аппки — каталог"
      >
        <BrandMark />
        <span>
          {config?.brand || "Аппки"}
          <span className="brand-dot">.</span>
        </span>
      </button>
    </header>
  );
}
function Navigation() {
  const { path, navigate } = useApp();
  return (
    <nav className="bottom-nav" aria-label="Основная навигация">
      {[
        {
          route: "/",
          icon: "grid",
          text: "Каталог",
          active:
            path === "/" ||
            path.startsWith("/apps/") ||
            path.startsWith("/payment"),
        },
        {
          route: "/how-it-works",
          icon: "guide",
          text: "Как это работает",
          active: path === "/how-it-works",
        },
        {
          route: "/orders",
          icon: "stack",
          text: "Мои приложения",
          active: path.startsWith("/orders"),
        },
        {
          route: "/help",
          icon: "help",
          text: "Помощь",
          active: path === "/help",
        },
      ].map((item) => (
        <button
          type="button"
          key={item.route}
          aria-current={item.active ? "page" : undefined}
          className={item.active ? "active" : ""}
          onClick={() => navigate(item.route)}
        >
          <Icon name={item.icon} size={22} />
          <span>{item.text}</span>
        </button>
      ))}
    </nav>
  );
}
function CartSummary() {
  const { apps, cart, path, checkout } = useApp();
  const selected = apps
    .filter(canPurchaseApp)
    .filter((app) => cart.includes(app.slug));
  if (!selected.length || (path !== "/" && !path.startsWith("/apps/")))
    return null;
  return (
    <div className="cart-summary" aria-label="Корзина">
      <div className="cart-summary-copy" aria-live="polite" aria-atomic="true">
        <span className="cart-summary-count">{appCount(selected.length)}</span>
        <strong className="cart-summary-total">
          {rubles(cartAmount(selected))}
        </strong>
      </div>
      <button
        type="button"
        className="button"
        onClick={(event) => checkout(event.currentTarget)}
      >
        Перейти к оплате <Icon name="arrow" size={17} />
      </button>
    </div>
  );
}
function Specs({ app }: { app: AppInfo }) {
  return (
    <dl className="app-specs">
      <div>
        <dt>Версия</dt>
        <dd>{app.version || "Уточняется"}</dd>
      </div>
      <div>
        <dt>Размер</dt>
        <dd>{app.size_bytes ? fileSize(app.size_bytes) : "Уточняется"}</dd>
      </div>
      <div>
        <dt>Совместимость</dt>
        <dd>{app.minimum_ios ? `iOS ${app.minimum_ios}+` : "Уточняется"}</dd>
      </div>
    </dl>
  );
}
function Catalog() {
  const { apps, cart, cartReady, toggleCart, checkout, path } = useApp();
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState("Все");
  const [expanded, setExpanded] = useState<string | null>(null);
  useEffect(() => {
    if (path.startsWith("/apps/")) setExpanded(routePart(path.slice(6)));
  }, [path]);
  const categories = ["Все", ...new Set(apps.map((app) => app.category))];
  const filtered = apps.filter(
    (app) =>
      (category === "Все" || app.category === category) &&
      `${app.title} ${app.subtitle}`
        .toLocaleLowerCase()
        .includes(query.trim().toLocaleLowerCase()),
  );
  return (
    <section aria-label="Каталог приложений">
      <div className="catalog-tools">
        <label className="search">
          <Icon name="search" size={18} />
          <input
            type="search"
            aria-label="Найти приложение"
            placeholder="Найти приложение"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
        </label>
        <div className="chips" role="group" aria-label="Категории приложений">
          {categories.map((item) => (
            <button
              type="button"
              key={item}
              className={`chip ${category === item ? "selected" : ""}`}
              aria-pressed={category === item}
              onClick={() => setCategory(item)}
            >
              {item}
              {item === "Все" && <span>{apps.length}</span>}
            </button>
          ))}
        </div>
      </div>
      <div className="app-list">
        {filtered.map((app) => {
          const selected = cart.includes(app.slug);
          const open = expanded === app.slug;
          return (
            <article className="app-card" key={app.slug} data-expanded={open}>
              <div className="app-card-row">
                <AppIcon slug={app.slug} title={app.title} />
                <div className="app-card-copy">
                  <div className="app-card-title">
                    <strong>{app.title}</strong>
                    {app.coming_soon && (
                      <span className="coming-soon-badge">Скоро</span>
                    )}
                  </div>
                  <span>
                    {app.coming_soon
                      ? "Готовим к запуску"
                      : !app.available
                        ? "Временно недоступно"
                        : app.category === "VPN"
                          ? "VPN-клиент"
                          : app.subtitle}
                  </span>
                </div>
                <div className="card-actions">
                  {!app.coming_soon && (
                    <button
                      type="button"
                      className="cart-toggle"
                      aria-label={`${selected ? "Убрать" : "Добавить"} ${app.title} ${selected ? "из корзины" : "в корзину"}`}
                      aria-pressed={selected}
                      disabled={!cartReady || !canPurchaseApp(app)}
                      onClick={() => toggleCart(app.slug)}
                    >
                      <Icon name="basket" size={10.5} />
                    </button>
                  )}
                  <button
                    type="button"
                    className="card-expand"
                    aria-label={`${open ? "Свернуть" : "Раскрыть"} карточку ${app.title}`}
                    aria-expanded={open}
                    aria-controls={`app-${app.slug}-details`}
                    onClick={() => setExpanded(open ? null : app.slug)}
                  >
                    <Icon name="chevronRight" size={20} />
                  </button>
                </div>
              </div>
              <div
                id={`app-${app.slug}-details`}
                className="card-dropdown"
                aria-hidden={!open}
                inert={!open}
              >
                <div className="card-dropdown-inner">
                  <div className="card-dropdown-content">
                    <p className="card-description">{app.description}</p>
                    {app.coming_soon ? (
                      <p className="coming-soon-note">
                        Установка станет доступна после запуска.
                      </p>
                    ) : (
                      <>
                        <Specs app={app} />
                        {app.price_rub !== null && (
                          <div className="card-price">
                            <span>Доступ к установке</span>
                            <strong>{rubles(app.price_rub)}</strong>
                          </div>
                        )}
                        <button
                          type="button"
                          className="button card-cta"
                          disabled={!canPurchaseApp(app) || !cartReady}
                          onClick={(event) =>
                            selected
                              ? checkout(event.currentTarget)
                              : toggleCart(app.slug)
                          }
                        >
                          {!canPurchaseApp(app)
                            ? "Временно недоступно"
                            : selected
                              ? "Перейти к оплате"
                              : "Добавить в корзину"}
                          <Icon
                            name={selected ? "arrow" : "basket"}
                            size={selected ? 17 : 10.5}
                          />
                        </button>
                      </>
                    )}
                  </div>
                </div>
              </div>
            </article>
          );
        })}
      </div>
      {filtered.length === 0 && (
        <div className="empty-state">
          <h2>Ничего не найдено</h2>
          <p>Попробуйте другое название или категорию.</p>
        </div>
      )}
    </section>
  );
}

function InstallationInstructions() {
  return (
    <div className="instruction-copy">
      <section>
        <h3>1. Установка приложения</h3>
        <p>
          После оплаты откройте «Мои приложения», выберите приложение и нажмите
          «Установить». Подтвердите установку на iPhone. Если окно не появилось,
          откройте установочную ссылку в Safari. На установку даётся 12 часов с
          момента оплаты. После установки нажмите «Приложение установлено»:
          установочная ссылка закроется и начнётся активация.
        </p>
      </section>
      <section>
        <h3>2. Активация</h3>
        <p>
          Для первого запуска может понадобиться авторизация установочного Apple
          Account. Инструкция и помощь доступны в оплаченном заказе. Вход
          выполняется только в App Store или «Контент и покупки», без выхода из
          основного iCloud. На активацию даётся 6 часов после отметки об
          установке. Запросите код и напишите в поддержку — сейчас код
          передаётся вручную. Если код уже не подходит, запросите новый.
        </p>
      </section>
      <section>
        <h3>3. Первый запуск</h3>
        <p>
          После успешного запуска вернитесь в свой аккаунт App Store. Установка
          не переносит приложение в покупки вашего Apple Account. Подписки
          сторонних сервисов и VPN-серверы приобретаются отдельно. Одна покупка
          рассчитана на одну установку. Для другого устройства или новой
          установки оформите отдельную покупку в каталоге.
        </p>
      </section>
    </div>
  );
}
function HowItWorks() {
  return (
    <section>
      <div className="page-heading">
        <h1>Как это работает</h1>
        <p>Установка и активация</p>
      </div>
      <InstallationInstructions />
    </section>
  );
}
function CheckoutDialog({
  selection,
  onClose,
  reloadCatalog,
}: {
  selection: PurchasableApp[];
  onClose: () => void;
  reloadCatalog: () => Promise<void>;
}) {
  const {
    config,
    user,
    ordersReady,
    ordersError,
    refreshOrders,
    recordOrder,
    navigate,
    toast,
  } = useApp();
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const sheet = useRef<HTMLElement>(null);
  const close = useRef<HTMLButtonElement>(null);
  const locked = useRef(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    close.current?.focus();
    return () => {
      mounted.current = false;
    };
  }, []);
  useEffect(() => {
    setConfirmed(false);
  }, [selection]);
  useEffect(() => {
    const element = sheet.current;
    if (!element) return;
    const keys = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !locked.current) {
        event.preventDefault();
        onClose();
      }
      if (event.key !== "Tab") return;
      const controls = [
        ...element.querySelectorAll<HTMLElement>(
          "button:not(:disabled),input:not(:disabled),a[href]",
        ),
      ].filter((item) => item.getClientRects().length);
      const first = controls[0],
        last = controls[controls.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last?.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first?.focus();
      }
    };
    element.addEventListener("keydown", keys);
    return () => element.removeEventListener("keydown", keys);
  }, [onClose]);
  async function pay() {
    if (
      !selection.length ||
      !ordersReady ||
      ordersError ||
      !confirmed ||
      !user ||
      !config?.payments_configured ||
      locked.current
    )
      return;
    locked.current = true;
    setBusy(true);
    setError("");
    const slugs = selection.map((app) => app.slug);
    const requestKey = cartRequestKey(slugs, config.payments_mode);
    const requestId = purchaseRequestId(requestKey);
    try {
      const order = await api<Order>("/orders", {
        method: "POST",
        body: JSON.stringify({
          app_slugs: slugs,
          expected_amount_rub: cartAmount(selection),
          request_id: requestId,
        }),
      });
      bindPurchaseRequest(requestKey, order.id, requestId);
      recordOrder(order);
      if (!mounted.current) return;
      onClose();
      if (order.status === "succeeded") {
        navigate("/orders");
        toast("Оплата прошла! Можно устанавливать.", "success");
      } else {
        navigate(`/payment/${encodeURIComponent(order.id)}`);
        if (
          order.status === "pending" &&
          order.has_payment &&
          !order.requires_payment_review &&
          order.payment_url
        )
          external(order.payment_url);
      }
    } catch (cause) {
      if (!mounted.current) return;
      setError(message(cause));
      setConfirmed(false);
      await reloadCatalog();
    } finally {
      locked.current = false;
      setBusy(false);
    }
  }
  return (
    <div
      className="instruction-overlay"
      onClick={(event) => {
        if (event.target === event.currentTarget && !busy) onClose();
      }}
    >
      <section
        className="instruction-sheet"
        role="dialog"
        aria-modal="true"
        aria-labelledby="instruction-title"
        ref={sheet}
      >
        <header className="instruction-header">
          <h2 id="instruction-title">Инструкция</h2>
          <button
            type="button"
            className="close-button"
            aria-label="Закрыть инструкцию"
            ref={close}
            disabled={busy}
            onClick={onClose}
          >
            <Icon name="close" size={18} />
          </button>
        </header>
        <div className="instruction-body">
          <p className="muted small-text">Перед покупкой</p>
          <h2>Установка и активация</h2>
          <InstallationInstructions />
          <div className="checkout-review">
            <h2>Ваш заказ</h2>
            <p className="muted small-text">Доступ к установке приложений</p>
            <div className="checkout-items">
              {selection.map((app) => (
                <div className="checkout-item" key={app.slug}>
                  <AppIcon slug={app.slug} title={app.title} />
                  <strong>{app.title}</strong>
                  <span>{rubles(app.price_rub)}</span>
                </div>
              ))}
            </div>
            <div className="checkout-total">
              <span>К оплате</span>
              <strong>{rubles(cartAmount(selection))}</strong>
            </div>
            {!user ? (
              <TelegramGate />
            ) : !config?.payments_configured ? (
              <Notice>Приём платежей ещё настраивается.</Notice>
            ) : null}
            {error && <Notice tone="error">{error}</Notice>}
            {user && !ordersReady && !ordersError && (
              <Notice>Проверяем ваши покупки…</Notice>
            )}
            {user && ordersError && (
              <>
                <Notice tone="error">
                  Не удалось проверить ваши покупки. {ordersError}
                </Notice>
                <Button secondary onClick={() => void refreshOrders()}>
                  Повторить проверку
                </Button>
              </>
            )}
            <label className="checkout-confirm">
              <input
                type="checkbox"
                checked={confirmed}
                disabled={busy}
                onChange={(event) => setConfirmed(event.target.checked)}
              />
              <span>Всё понятно, всё верно</span>
            </label>
            <Button
              className="full"
              disabled={
                !selection.length ||
                !ordersReady ||
                Boolean(ordersError) ||
                !confirmed ||
                !user ||
                !config?.payments_configured
              }
              busy={busy}
              onClick={pay}
            >
              Оплатить {rubles(cartAmount(selection))}
              <Icon name="arrow" size={17} />
            </Button>
          </div>
        </div>
      </section>
    </div>
  );
}

function PurchasedAppRow({
  order,
  item,
  navigate,
}: {
  order: Order;
  item: OrderItem;
  navigate: (path: string) => void;
}) {
  const now = useServerNow(item.server_now);
  const status = itemAccess(item, now);
  if (status === "completed")
    return (
      <article className="order-row completed-purchase">
        <div className="completed-app-icon">
          <AppIcon slug={item.app_slug} title={item.app_title} />
          <span className="completed-app-check">
            <Icon name="check" size={12} />
          </span>
        </div>
        <div className="order-row-info">
          <strong>{item.app_title}</strong>
          <span className="completed-purchase-title">
            Приложение установлено!
          </span>
          <span>Всё готово к использованию.</span>
        </div>
      </article>
    );
  return (
    <button
      type="button"
      className="order-row"
      aria-label={`Открыть ${item.app_title}, покупка ${formatDate(order.paid_at || order.created_at)}`}
      onClick={() => navigate(routeForItem(order.id, item.app_slug))}
    >
      <AppIcon slug={item.app_slug} title={item.app_title} />
      <div className="order-row-info">
        <strong>{item.app_title}</strong>
        <span>{accessLabel(status)}</span>
        <span>
          Покупка {formatDate(order.paid_at || order.created_at)},{" "}
          {formatTime(order.paid_at || order.created_at)}
        </span>
        {(status === "install" || status === "activate") && (
          <span>
            Осталось{" "}
            {countdown(
              remainingSeconds(
                status === "install"
                  ? item.install_expires_at
                  : item.activation_expires_at,
                now,
              ),
            )}
          </span>
        )}
      </div>
      <Icon name="chevronRight" size={18} />
    </button>
  );
}

function Orders() {
  const {
    user,
    authReady,
    orders,
    ordersLoading,
    ordersError,
    refreshOrders,
    config,
    navigate,
  } = useApp();
  useEffect(() => {
    void refreshOrders();
    const refresh = () => {
      if (document.visibilityState === "visible") void refreshOrders();
    };
    const timer = window.setInterval(refresh, 30000);
    window.addEventListener("focus", refresh);
    document.addEventListener("visibilitychange", refresh);
    return () => {
      clearInterval(timer);
      window.removeEventListener("focus", refresh);
      document.removeEventListener("visibilitychange", refresh);
    };
  }, [refreshOrders]);
  const owned = purchasedApps(orders, config?.payments_mode ?? "test");
  return (
    <section>
      <div className="page-heading">
        <h1>Мои приложения</h1>
        <p>Каждая покупка — отдельная установка и активация.</p>
      </div>
      {!authReady || (user && ordersLoading && !orders.length) ? (
        <Skeleton />
      ) : !user ? (
        <TelegramGate />
      ) : (
        <>
          {ordersError && (
            <>
              <Notice tone="error">{ordersError}</Notice>
              <Button secondary onClick={() => void refreshOrders()}>
                Попробовать ещё раз
              </Button>
            </>
          )}
          <div className="orders-list">
            {owned.map(({ order, item }) => (
              <PurchasedAppRow
                key={`${order.id}:${item.app_slug}`}
                order={order}
                item={item}
                navigate={navigate}
              />
            ))}
          </div>
          {!owned.length && !ordersError && (
            <div className="empty-state">
              <p>Здесь появятся приобретённые приложения.</p>
            </div>
          )}
        </>
      )}
    </section>
  );
}

function SecretField({
  label,
  value,
  password = false,
}: {
  label: string;
  value: string;
  password?: boolean;
}) {
  const { toast } = useApp();
  const [visible, setVisible] = useState(!password);
  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      toast(`${label} скопирован`);
    } catch {
      setVisible(true);
      toast("Не удалось скопировать. Выделите значение вручную.");
    }
  }
  return (
    <label className="secret-field">
      <span>{label}</span>
      <div>
        <input
          readOnly
          type={visible ? "text" : "password"}
          value={value}
          autoComplete="off"
          aria-label={label}
        />
        {password && (
          <button
            type="button"
            onClick={() => setVisible(!visible)}
            aria-label={visible ? "Скрыть пароль" : "Показать пароль"}
          >
            <Icon name="eye" size={18} />
          </button>
        )}
        <button
          type="button"
          aria-label={`Скопировать ${label.toLowerCase()}`}
          onClick={copy}
        >
          <Icon name="copy" size={18} />
        </button>
      </div>
    </label>
  );
}
function FlowInstructions({
  name,
  activation = false,
  credentials = false,
}: {
  name: string;
  activation?: boolean;
  credentials?: boolean;
}) {
  return (
    <section
      className="flow-instructions"
      aria-label={activation ? "Инструкция активации" : "Инструкция установки"}
    >
      <ol>
        {activation ? (
          <>
            <li>
              {credentials
                ? "Используйте данные для входа ниже."
                : "Получите в поддержке данные для входа в App Store."}
            </li>
            <li>
              Войдите только в «Контент и покупки». Основной iCloud не меняйте.
              Если нужен код, запросите его ниже и получите в поддержке.
            </li>
            <li>
              Откройте {name}, затем вернитесь в свой App Store и нажмите
              «Приложение запустилось».
            </li>
          </>
        ) : (
          <>
            <li>Нажмите «Установить приложение».</li>
            <li>
              Подтвердите установку в системном окне iPhone. Если оно не
              появилось, откройте ссылку в Safari.
            </li>
            <li>
              Дождитесь иконки {name} на экране «Домой», затем нажмите
              «Приложение установлено».
            </li>
          </>
        )}
      </ol>
    </section>
  );
}

function ActivationPanel({
  order,
  item,
  onComplete,
  onRefresh,
}: {
  order: Order;
  item: OrderItem;
  onComplete: () => Promise<void>;
  onRefresh: () => Promise<void>;
}) {
  const { config } = useApp();
  const [data, setData] = useState<Activation | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [suspended, setSuspended] = useState(false);
  const request = useRef(0);
  const locked = useRef(false);
  const expiredRefresh = useRef(false);
  const retryRefresh = useRef<string | null>(null);
  const path = `/orders/${encodeURIComponent(order.id)}/items/${encodeURIComponent(item.app_slug)}/activation`;
  const load = useCallback(
    async (quiet = false) => {
      if (locked.current) return;
      locked.current = true;
      const version = ++request.current;
      setBusy(true);
      if (!quiet) setData(null);
      setError("");
      try {
        const fresh = await api<Activation>(path);
        if (request.current !== version) return;
        setData(fresh);
        setSuspended(false);
      } catch (cause) {
        if (request.current !== version) return;
        setError(message(cause));
        if (
          cause instanceof ApiError &&
          (cause.status === 403 || cause.status === 409 || cause.status === 410)
        ) {
          setData(null);
          void onRefresh();
        }
      } finally {
        locked.current = false;
        if (request.current === version) setBusy(false);
      }
    },
    [path, onRefresh],
  );
  useEffect(() => {
    void load();
    const refresh = () => {
      if (document.visibilityState === "visible") void load();
      else {
        request.current++;
        setSuspended(true);
        setData(null);
      }
    };
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") void load(true);
    }, 15000);
    document.addEventListener("visibilitychange", refresh);
    window.addEventListener("focus", refresh);
    return () => {
      request.current++;
      clearInterval(timer);
      document.removeEventListener("visibilitychange", refresh);
      window.removeEventListener("focus", refresh);
    };
  }, [load]);
  const now = useServerNow(data?.server_now || item.server_now);
  const expiresAt = data?.activation_expires_at || item.activation_expires_at;
  const remaining = remainingSeconds(expiresAt, now);
  const active = !suspended && item.can_activate && remaining > 0;
  const retrySeconds = remainingSeconds(
    data?.code_request?.retry_at || null,
    now,
  );
  useEffect(() => {
    if (!remaining && !expiredRefresh.current) {
      expiredRefresh.current = true;
      request.current++;
      setData(null);
      void onRefresh();
    }
  }, [remaining, onRefresh]);
  useEffect(() => {
    const id = data?.code_request?.id;
    if (
      active &&
      id &&
      !data.can_request_code &&
      !retrySeconds &&
      retryRefresh.current !== id
    ) {
      retryRefresh.current = id;
      void load(true);
    }
  }, [active, data, retrySeconds, load]);
  const support = supportURL(data?.support_url || config?.support_url);
  async function requestCode() {
    if (!active || !data?.can_request_code || retrySeconds || locked.current)
      return;
    locked.current = true;
    const version = request.current;
    setBusy(true);
    setError("");
    try {
      const next = await api<ActivationRequestState>(`${path}/code-request`, {
        method: "POST",
      });
      if (request.current === version)
        setData((previous) => (previous ? { ...previous, ...next } : null));
    } catch (cause) {
      if (request.current === version) {
        setError(message(cause));
        if (
          cause instanceof ApiError &&
          (cause.status === 403 || cause.status === 409 || cause.status === 410)
        ) {
          setData(null);
          void onRefresh();
        }
      }
    } finally {
      locked.current = false;
      if (request.current === version) setBusy(false);
    }
  }
  async function complete() {
    if (!active || locked.current) return;
    locked.current = true;
    setBusy(true);
    setError("");
    try {
      await onComplete();
    } catch (cause) {
      setError(message(cause));
      if (
        cause instanceof ApiError &&
        (cause.status === 403 || cause.status === 409 || cause.status === 410)
      ) {
        setData(null);
        void onRefresh();
      }
    } finally {
      locked.current = false;
      setBusy(false);
    }
  }
  return (
    <>
      <FlowInstructions
        name={item.app_title}
        activation
        credentials={data?.mode === "credentials"}
      />
      <div className="surface activation-panel">
        <h2>Активируйте приложение</h2>
        {active && <AccessDeadline activation deadline={expiresAt} now={now} />}
        {suspended ? (
          <Skeleton />
        ) : !active ? (
          <Notice>
            Время на активацию истекло. Для новой установки оформите отдельную
            покупку в каталоге.
          </Notice>
        ) : (
          <>
            {busy && !data ? <Skeleton /> : null}
            {error && <Notice tone="error">{error}</Notice>}
            {!data && !busy && (
              <Button secondary onClick={() => load()}>
                Обновить доступ
              </Button>
            )}
            {data && (
              <>
                {data.instructions.trim() && (
                  <p className="activation-profile-instructions preserve-lines">
                    {data.instructions}
                  </p>
                )}
                {data.mode === "credentials" && (
                  <>
                    {data.apple_id && (
                      <SecretField
                        label="Apple Account"
                        value={data.apple_id}
                      />
                    )}
                    {data.password && (
                      <SecretField
                        label="Пароль"
                        value={data.password}
                        password
                      />
                    )}
                    <Notice>
                      Входите только в App Store («Контент и покупки»). Основной
                      iCloud менять не нужно. Код подтверждения вводите только в
                      системном окне Apple.
                    </Notice>
                  </>
                )}
                <Button
                  className="full"
                  secondary
                  busy={busy}
                  disabled={!data.can_request_code || retrySeconds > 0}
                  onClick={requestCode}
                >
                  {retrySeconds
                    ? `Новый запрос через ${countdown(retrySeconds).slice(3)}`
                    : data.code_request?.status === "manual_required"
                      ? "Запросить новый код"
                      : "Запросить код"}
                </Button>
                {data.code_request?.status === "manual_required" && (
                  <Notice>
                    Запрос сохранён. Напишите в поддержку, чтобы получить код.
                  </Notice>
                )}
                {support ? (
                  <a
                    className="button secondary full"
                    href={support}
                    target="_blank"
                    rel="noreferrer"
                  >
                    Написать в поддержку <Icon name="external" size={17} />
                  </a>
                ) : (
                  <p className="muted small-text">
                    Канал поддержки ещё настраивается.
                  </p>
                )}
                <p className="muted small-text">
                  Код передаётся вручную через поддержку. Если он уже не
                  подходит, запросите новый. Повторный запрос не продлевает
                  время активации.
                </p>
                <Button className="full" busy={busy} onClick={complete}>
                  Приложение запустилось <Icon name="check" size={18} />
                </Button>
                <p className="muted small-text">
                  Нажмите после успешного запуска. Этот шаг сохраняет вашу
                  отметку о завершении.
                </p>
              </>
            )}
          </>
        )}
      </div>
    </>
  );
}

function OrderPage({
  id,
  slug,
  paymentReturn = false,
  paymentPage = false,
}: {
  id: string;
  slug?: string;
  paymentReturn?: boolean;
  paymentPage?: boolean;
}) {
  const { config, user, authReady, recordOrder, navigate, toast } = useApp();
  const [order, setOrder] = useState<Order | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [checking, setChecking] = useState(false);
  const [grant, setGrant] = useState<Grant | null>(null);
  const [grantBusy, setGrantBusy] = useState(false);
  const [grantError, setGrantError] = useState("");
  const [accessFresh, setAccessFresh] = useState(true);
  const loadingPayment = useRef(false);
  const stateVersion = useRef(0);
  const load = useCallback(
    async (quiet = false) => {
      if (!user || !id || loadingPayment.current) {
        if (!user || !id) setLoading(false);
        return;
      }
      loadingPayment.current = true;
      if (!quiet) setChecking(true);
      const version = stateVersion.current;
      try {
        const fresh = await api<Order>(`/orders/${encodeURIComponent(id)}`);
        if (version !== stateVersion.current) return;
        setOrder(fresh);
        recordOrder(fresh);
        setError("");
        if ((paymentReturn || paymentPage) && fresh.status === "succeeded") {
          navigate("/orders");
          toast("Оплата прошла! Можно устанавливать.", "success");
        }
        setAccessFresh(true);
      } catch (cause) {
        setError(message(cause));
      } finally {
        setLoading(false);
        setChecking(false);
        loadingPayment.current = false;
      }
    },
    [id, user, recordOrder, paymentReturn, paymentPage, navigate, toast, slug],
  );
  useEffect(() => {
    void load();
  }, [load]);
  useEffect(() => {
    if (order?.status !== "pending" || !order.has_payment) return;
    const refresh = () => {
      if (document.visibilityState === "visible") void load(true);
    };
    const timer = window.setInterval(refresh, 8000);
    document.addEventListener("visibilitychange", refresh);
    window.addEventListener("focus", refresh);
    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", refresh);
      window.removeEventListener("focus", refresh);
    };
  }, [order?.status, order?.has_payment, load]);
  async function retryPayment() {
    if (
      !order ||
      order.status !== "pending" ||
      (!order.has_payment && !order.can_retry_payment) ||
      order.requires_payment_review ||
      loadingPayment.current
    )
      return;
    loadingPayment.current = true;
    setChecking(true);
    setError("");
    try {
      const next = await api<Order>(
        `/orders/${encodeURIComponent(order.id)}/retry-payment`,
        { method: "POST" },
      );
      setOrder(next);
      recordOrder(next);
      if (next.status === "succeeded") {
        navigate("/orders");
        toast("Оплата прошла! Можно устанавливать.", "success");
      } else if (
        next.status === "pending" &&
        next.has_payment &&
        !next.requires_payment_review &&
        next.payment_url
      )
        external(next.payment_url);
    } catch (cause) {
      setError(message(cause));
      if (cause instanceof ApiError && cause.status === 409) {
        try {
          const fresh = await api<Order>(
            `/orders/${encodeURIComponent(order.id)}`,
          );
          setOrder(fresh);
          recordOrder(fresh);
          if (fresh.status === "succeeded") {
            navigate("/orders");
            toast("Оплата прошла! Можно устанавливать.", "success");
          }
        } catch {
          /* Keep the original retry error if the status refresh also fails. */
        }
      }
    } finally {
      setChecking(false);
      loadingPayment.current = false;
    }
  }
  const items = order ? orderItems(order) : [];
  const selected =
    items.find((item) => item.app_slug === slug) ??
    (items.length === 1 ? items[0] : null);
  const now = useServerNow(selected?.server_now || items[0]?.server_now || "");
  const access = selected ? itemAccess(selected, now) : "unavailable";
  const accessRef = useRef({ selected, now, fresh: accessFresh });
  accessRef.current = { selected, now, fresh: accessFresh };
  useEffect(() => {
    if (order?.status !== "succeeded") return;
    const refresh = () => {
      setAccessFresh(false);
      setGrant(null);
      if (document.visibilityState === "visible") void load(true);
    };
    const timer = window.setInterval(() => {
      if (
        document.visibilityState === "visible" &&
        items.some(
          (item) =>
            item.access_status === "install" ||
            item.access_status === "activate",
        )
      )
        void load(true);
    }, 15000);
    window.addEventListener("focus", refresh);
    document.addEventListener("visibilitychange", refresh);
    return () => {
      clearInterval(timer);
      window.removeEventListener("focus", refresh);
      document.removeEventListener("visibilitychange", refresh);
    };
  }, [order?.status, items, load]);
  useEffect(() => {
    if (access !== "install") setGrant(null);
  }, [access]);
  function installationAllowed() {
    const current = accessRef.current;
    return (
      current.fresh &&
      current.selected &&
      itemAccess(current.selected, current.now) === "install"
    );
  }
  async function markState(update: {
    installed?: boolean;
    activated?: boolean;
  }) {
    if (!order || !selected) return;
    const next = await api<OrderItem>(
      `/orders/${encodeURIComponent(order.id)}/items/${encodeURIComponent(selected.app_slug)}/state`,
      { method: "PATCH", body: JSON.stringify(update) },
    );
    const changed = {
      ...order,
      items: items.map((item) =>
        item.app_slug === next.app_slug ? next : item,
      ),
    };
    stateVersion.current++;
    setAccessFresh(true);
    setOrder(changed);
    recordOrder(changed);
    if (update.installed) setGrant(null);
    if (update.activated) {
      toast("Всё готово! Приложение установлено и активировано.", "success");
    }
  }
  async function installLink() {
    if (!order || !selected || grantBusy || !installationAllowed()) return;
    setGrantBusy(true);
    setGrantError("");
    try {
      if (
        grant &&
        remainingSeconds(grant.expires_at, accessRef.current.now) > 0
      ) {
        external(grant.url);
        return;
      }
      const next = await api<Grant>(
        `/orders/${encodeURIComponent(order.id)}/items/${encodeURIComponent(selected.app_slug)}/install-link`,
        { method: "POST" },
      );
      const url = httpsURL(next.url);
      if (!url || new URL(url).origin !== window.location.origin)
        throw new Error(
          "Сервер вернул некорректную ссылку установки. Обратитесь в поддержку.",
        );
      if (!installationAllowed()) return;
      setGrant(next);
      external(url);
    } catch (cause) {
      setGrantError(message(cause));
      if (
        cause instanceof ApiError &&
        (cause.status === 409 || cause.status === 410)
      )
        void load(true);
    } finally {
      setGrantBusy(false);
    }
  }
  async function installed() {
    if (!installationAllowed() || grantBusy) return;
    setGrantBusy(true);
    setGrantError("");
    try {
      await markState({ installed: true });
    } catch (cause) {
      setGrantError(message(cause));
    } finally {
      setGrantBusy(false);
    }
  }
  async function copyLink() {
    if (!grant || !installationAllowed()) return;
    if (!remainingSeconds(grant.expires_at, accessRef.current.now)) {
      setGrant(null);
      toast("Ссылка истекла. Нажмите «Установить приложение».");
      return;
    }
    try {
      await navigator.clipboard.writeText(grant.url);
      toast("Ссылка скопирована. Откройте её в Safari на iPhone.");
    } catch {
      toast("Не удалось скопировать. Нажмите «Установить приложение».");
    }
  }
  const paymentScreen =
    paymentPage ||
    paymentReturn ||
    (order !== null && order.status !== "succeeded");
  if (!authReady || (user && loading))
    return (
      <>
        {paymentScreen ? (
          <Back />
        ) : (
          <Back label="Мои приложения" to="/orders" />
        )}
        <Skeleton />
      </>
    );
  if (!user)
    return (
      <section>
        {paymentScreen && <Back />}
        <div className="page-heading">
          <h1>{paymentReturn ? "Проверим оплату в Telegram" : "Ваш заказ"}</h1>
          <p>
            {paymentReturn
              ? "Откройте мини-приложение из бота. Сервер проверит платёж и покажет доступ в «Моих приложениях»."
              : "Откройте бота, чтобы увидеть свой заказ."}
          </p>
        </div>
        <TelegramGate />
      </section>
    );
  if (!order)
    return (
      <>
        {paymentScreen ? (
          <Back />
        ) : (
          <Back label="Мои приложения" to="/orders" />
        )}
        <Notice tone="error">{error || "Заказ не найден."}</Notice>
        <Button secondary busy={checking} onClick={() => void load()}>
          Проверить ещё раз
        </Button>
      </>
    );
  const paymentURL = httpsURL(order.payment_url);
  const support = supportURL(config?.support_url);
  if (order.status !== "succeeded")
    return (
      <section>
        <Back />
        <div className="page-heading">
          <h1>
            {order.status === "pending" ? "Оплата" : orderStatus(order.status)}
          </h1>
        </div>
        <div className="payment-amount">{rubles(order.amount_rub)}</div>
        <p className="muted">
          Доступ к {items.map((item) => item.app_title).join(", ")}
        </p>
        {error && <Notice tone="error">{error}</Notice>}
        {order.status === "pending" ? (
          <div className="surface payment-panel">
            <h2>
              {order.requires_payment_review
                ? "Нужно уточнить статус оплаты"
                : order.has_payment && paymentReturn
                  ? "Проверяем результат оплаты"
                  : order.has_payment
                    ? "Ожидаем оплату"
                    : "Создание платежа"}
            </h2>
            {order.requires_payment_review ? (
              <>
                <p>
                  Сервер не получил подтверждение создания платежа. С момента
                  попытки прошло больше 23 часов — сначала нужно уточнить её
                  результат в поддержке. Не оплачивайте этот заказ повторно до
                  проверки.
                </p>
                {support ? (
                  <a
                    className="button full"
                    href={support}
                    target="_blank"
                    rel="noreferrer"
                  >
                    Написать в поддержку <Icon name="external" size={17} />
                  </a>
                ) : (
                  <Notice>
                    Канал поддержки ещё настраивается. Не повторяйте оплату до
                    проверки.
                  </Notice>
                )}
              </>
            ) : order.has_payment ? (
              <>
                <p>После оплаты приложения появятся в «Моих приложениях».</p>
                {paymentURL ? (
                  <Button
                    className="full"
                    busy={checking}
                    onClick={retryPayment}
                  >
                    Открыть оплату
                    <Icon name="external" size={17} />
                  </Button>
                ) : (
                  <Notice>
                    Платёж создан, но ссылка на оплату пока недоступна.
                    Проверьте его статус позже.
                  </Notice>
                )}
                <Button
                  className="full"
                  secondary
                  busy={checking}
                  onClick={() => void load()}
                >
                  Проверить оплату <Icon name="refresh" size={17} />
                </Button>
                <p className="small-text muted">
                  Статус проверяется автоматически.
                </p>
              </>
            ) : order.can_retry_payment ? (
              <>
                <Notice>
                  Создание платежа пока не подтверждено. Повторим ту же попытку
                  для этого заказа, сохранив её идентификатор.
                </Notice>
                <Button className="full" busy={checking} onClick={retryPayment}>
                  Повторить создание оплаты
                </Button>
              </>
            ) : (
              <Notice>
                Создание платежа сейчас недоступно. Обратитесь в поддержку.
              </Notice>
            )}
          </div>
        ) : (
          <div className="surface">
            <h2>
              {order.status === "refunded"
                ? "Доступ закрыт после возврата"
                : "Оплата не завершена"}
            </h2>
            <p>
              {order.status === "refunded"
                ? "Если есть вопросы, обратитесь в поддержку."
                : "Корзина сохранена. Вы можете оформить новый заказ."}
            </p>
            <Button
              onClick={() =>
                navigate(order.status === "refunded" ? "/help" : "/")
              }
            >
              {order.status === "refunded"
                ? "Открыть помощь"
                : "Вернуться в каталог"}
            </Button>
          </div>
        )}
      </section>
    );
  if (!selected)
    return (
      <section>
        <Back label="Мои приложения" to="/orders" />
        <div className="page-heading">
          <h1>Приложения в заказе</h1>
        </div>
        <div className="orders-list">
          {items.map((item) => (
            <PurchasedAppRow
              key={`${order.id}:${item.app_slug}`}
              order={order}
              item={item}
              navigate={navigate}
            />
          ))}
        </div>
      </section>
    );
  const hasValidGrant =
    grant &&
    remainingSeconds(grant.expires_at, now) > 0 &&
    access === "install";
  const expired =
    access === "install_expired" || access === "activation_expired";
  return (
    <section>
      <Back label="Мои приложения" to="/orders" />
      <div className="flow-heading">
        <AppIcon slug={selected.app_slug} title={selected.app_title} large />
        <div>
          <h1>{selected.app_title}</h1>
          <p>{accessLabel(access)}</p>
        </div>
      </div>
      {error && <Notice tone="error">{error}</Notice>}
      {!accessFresh ? (
        <div className="surface">
          <p>Обновите доступ, чтобы продолжить.</p>
          <Button className="full" busy={checking} onClick={() => load(true)}>
            Обновить доступ
          </Button>
        </div>
      ) : access === "completed" ? (
        <div className="surface completion-success">
          <Icon name="check" size={28} />
          <h2>Всё готово! Приложение установлено и активировано.</h2>
          <Button className="full" onClick={() => navigate("/orders")}>
            Мои приложения <Icon name="arrow" size={18} />
          </Button>
        </div>
      ) : expired || access === "unavailable" ? (
        <div className="surface">
          <h2>{accessLabel(access)}</h2>
          <p>
            {expired
              ? "Окно этой покупки закрылось. Для новой установки оформите отдельную покупку в каталоге."
              : "Не удалось открыть доступ к приложению. Обратитесь в поддержку."}
          </p>
          {expired && (
            <Button
              className="full"
              onClick={() =>
                navigate(`/apps/${encodeURIComponent(selected.app_slug)}`)
              }
            >
              Купить ещё раз <Icon name="arrow" size={18} />
            </Button>
          )}
          {support && (
            <a
              className="button secondary full"
              href={support}
              target="_blank"
              rel="noreferrer"
            >
              Написать в поддержку
            </a>
          )}
        </div>
      ) : (
        <>
          <div
            className="flow-tabs"
            role="group"
            aria-label="Установка и активация"
          >
            <button
              type="button"
              aria-current={access === "install" ? "step" : undefined}
              data-done={selected.installed}
              disabled={selected.installed}
            >
              <span>1</span>Установка
            </button>
            <button
              type="button"
              disabled={!selected.installed}
              aria-current={access === "activate" ? "step" : undefined}
            >
              <span>2</span>Активация
            </button>
          </div>
          {access === "install" ? (
            <>
              <FlowInstructions name={selected.app_title} />
              <div className="surface installation-panel">
                <h2>Установите приложение</h2>
                <AccessDeadline
                  deadline={selected.install_expires_at}
                  now={now}
                />
                <p>Подтвердите установку в системном окне на iPhone.</p>
                <Button className="full" busy={grantBusy} onClick={installLink}>
                  Установить приложение <Icon name="arrow" size={17} />
                </Button>
                {hasValidGrant && (
                  <div className="grant">
                    <p className="muted small-text">
                      Нет окна установки? Откройте ссылку в Safari.
                    </p>
                    <Button className="full" secondary onClick={copyLink}>
                      Скопировать для Safari <Icon name="copy" size={17} />
                    </Button>
                    <p className="muted small-text">
                      Ссылка действует до {formatTime(grant.expires_at)}. Не
                      пересылайте её другим людям.
                    </p>
                  </div>
                )}
                {grantError && <Notice tone="error">{grantError}</Notice>}
                <Button
                  className="full"
                  secondary
                  busy={grantBusy}
                  onClick={installed}
                >
                  Приложение установлено <Icon name="check" size={18} />
                </Button>
                <p className="muted small-text">
                  Нажмите после установки. Ссылка закроется, и начнутся 6 часов
                  на активацию.
                </p>
              </div>
            </>
          ) : (
            <ActivationPanel
              key={`${order.id}:${selected.app_slug}`}
              order={order}
              item={selected}
              onRefresh={load}
              onComplete={() => markState({ activated: true })}
            />
          )}
        </>
      )}
    </section>
  );
}

const questions = [
  [
    "Что я получаю после оплаты?",
    "Доступ к установочной ссылке выбранного приложения и инструкции по первому запуску. Приложение не становится покупкой вашего Apple Account. Подписки сторонних сервисов и VPN-серверы в стоимость не входят.",
  ],
  [
    "Нужен ли компьютер или провод?",
    "Достаточно iPhone и Safari. Установите приложение в течение 12 часов после оплаты. После отметки «Приложение установлено» ссылка закрывается и начинается 6-часовое окно активации.",
  ],
  [
    "Почему приложение не открывается?",
    "После отметки об установке у вас есть 6 часов на активацию. Запросите код на шаге «Активация» и напишите в поддержку: сейчас код передаётся вручную. Если код уже не подходит, можно запросить новый; окно активации не продлевается.",
  ],
  [
    "Нужно ли выходить из iCloud?",
    "Нет. Если требуется авторизация, она выполняется только в App Store или «Контент и покупки». Основной аккаунт iCloud и «Локатор» менять не нужно. Никогда не вводите свой пароль Apple на странице сервиса.",
  ],
  [
    "Можно ли вернуться в свой аккаунт?",
    "После успешного запуска можно вернуться в собственный аккаунт App Store. Одна покупка даёт одну установку и активацию. Для другого устройства или новой установки оформите отдельную покупку; прежние покупки сохранятся в «Моих приложениях».",
  ],
  [
    "Что делать, если приложение уже установлено?",
    "Сначала сохраните его настройки. Система может потребовать удалить установленную копию; локальные данные при этом могут потеряться. Для VPN сохраните ссылку подписки и запасной способ доступа в интернет.",
  ],
  [
    "Как устроена оплата и возврат?",
    "После оплаты приложения появятся в «Моих приложениях». Если нужна помощь с установкой или возвратом, обратитесь в поддержку.",
  ],
];
function Help() {
  const { config } = useApp();
  const support = supportURL(config?.support_url);
  return (
    <section>
      <div className="page-heading">
        <h1>Помощь</h1>
        <p>От покупки до первого запуска.</p>
      </div>
      <div className="faq">
        {questions.map(([title, text]) => (
          <details key={title}>
            <summary>
              {title}
              <Icon name="down" size={18} />
            </summary>
            <p>{text}</p>
          </details>
        ))}
      </div>
      <div className="support-card">
        <h2>Нужна помощь?</h2>
        <p>Напишите, на каком шаге возник вопрос.</p>
        {support ? (
          <a className="button" href={support} target="_blank" rel="noreferrer">
            Написать в поддержку <Icon name="arrow" size={18} />
          </a>
        ) : (
          <p className="muted small-text">Канал поддержки ещё настраивается.</p>
        )}
      </div>
      <div className="terms">
        <h3>О сервисе</h3>
        <p>
          Аппки — сервис доставки приложений. Совместимость зависит от
          устройства, версии iOS и доступности авторизации Apple. Мы не связаны
          с Apple или разработчиками приложений.
        </p>
        <p>
          Не передавайте другим людям установочные ссылки и данные активации. Не
          отправляйте поддержке пароли и коды личного аккаунта.
        </p>
      </div>
    </section>
  );
}
function routePart(value: string): string {
  try {
    return decodeURIComponent(value);
  } catch {
    return "";
  }
}
function Route() {
  const { path } = useApp();
  if (path === "/") return null;
  if (path === "/orders") return <Orders />;
  if (path === "/how-it-works") return <HowItWorks />;
  if (path === "/help") return <Help />;
  if (path.startsWith("/apps/")) return null;
  if (path === "/payment/return")
    return (
      <OrderPage
        id={new URLSearchParams(window.location.search).get("order_id") ?? ""}
        paymentReturn
      />
    );
  if (path.startsWith("/payment/"))
    return <OrderPage key={path} id={routePart(path.slice(9))} paymentPage />;
  const itemRoute = /^\/orders\/([^/]+)\/apps\/([^/]+)$/.exec(path);
  if (itemRoute)
    return (
      <OrderPage
        key={path}
        id={routePart(itemRoute[1])}
        slug={routePart(itemRoute[2])}
      />
    );
  if (path.startsWith("/orders/"))
    return <OrderPage key={path} id={routePart(path.slice(8))} />;
  return (
    <>
      <Back />
      <div className="empty-state">
        <h1>Такой страницы нет</h1>
        <p>Все приложения доступны в каталоге.</p>
      </div>
    </>
  );
}

export default function App() {
  const [config, setConfig] = useState<Config | null>(null);
  const [apps, setApps] = useState<AppInfo[]>([]);
  const [user, setUser] = useState<User | null>(null);
  const [authReady, setAuthReady] = useState(false);
  const [authError, setAuthError] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [path, setPath] = useState(
    window.location.pathname.replace(/\/$/, "") || "/",
  );
  const [announcement, setAnnouncement] = useState("");
  const [announcementTone, setAnnouncementTone] = useState<
    "default" | "success"
  >("default");
  const showToast = useCallback(
    (text: string, tone: "default" | "success" = "default") => {
      setAnnouncementTone(tone);
      setAnnouncement(text);
    },
    [],
  );
  const [orders, setOrders] = useState<Order[]>([]);
  const [ordersLoading, setOrdersLoading] = useState(false);
  const [ordersReady, setOrdersReady] = useState(false);
  const [ordersError, setOrdersError] = useState("");
  const [cart, setCart] = useState<string[]>([]);
  const [cartReady, setCartReady] = useState(false);
  const cartVisible =
    (path === "/" || path.startsWith("/apps/")) &&
    apps.some((app) => canPurchaseApp(app) && cart.includes(app.slug));
  const cartRef = useRef<string[]>([]);
  const cartStorageKey = useRef("");
  const [selection, setSelection] = useState<PurchasableApp[] | null>(null);
  const checkoutAnchor = useRef<HTMLElement | null>(null);
  const main = useRef<HTMLElement>(null);
  const catalogScroll = useRef(0);
  const lastPath = useRef(path);
  const userRef = useRef<User | null>(null);
  const orderWrites = useRef(0);
  const orderWriteVersions = useRef(new Map<string, number>());
  const ordersRequest = useRef(0);
  userRef.current = user;
  const navigate = useCallback((route: string) => {
    if (window.location.pathname + window.location.search !== route)
      window.history.pushState(null, "", route);
    setPath(new URL(route, window.location.origin).pathname);
  }, []);
  const updateCart = useCallback((next: string[]) => {
    cartRef.current = next;
    setCart(next);
    try {
      if (cartStorageKey.current)
        sessionStorage.setItem(cartStorageKey.current, JSON.stringify(next));
    } catch {
      /* WebView storage is optional. */
    }
  }, []);
  const settleOrders = useCallback(
    (confirmed: Order[]) => {
      const purchased = settlePurchaseOrders(confirmed).flatMap(
        (settled) => settled.purchasedSlugs,
      );
      if (!purchased.length) return;
      updateCart(cartRef.current.filter((slug) => !purchased.includes(slug)));
      navigate("/orders");
      showToast("Оплата прошла! Можно устанавливать.", "success");
    },
    [updateCart, navigate, showToast],
  );
  const recordOrder = useCallback(
    (order: Order) => {
      orderWrites.current++;
      orderWriteVersions.current.set(order.id, orderWrites.current);
      setOrders((previous) => {
        const cached = previous.find((item) => item.id === order.id);
        if (order.status === "pending" && cached && cached.status !== "pending")
          return previous;
        return [order, ...previous.filter((item) => item.id !== order.id)].sort(
          (a, b) => b.created_at.localeCompare(a.created_at),
        );
      });
      settleOrders([order]);
    },
    [settleOrders],
  );
  const refreshOrders = useCallback(async () => {
    if (!user || !cartReady) return;
    const owner = user.id;
    const version = orderWrites.current;
    const request = ++ordersRequest.current;
    setOrdersLoading(true);
    setOrdersError("");
    try {
      const results = await Promise.allSettled([
        api<{ items: Order[] }>("/orders?status=succeeded"),
        api<{ items: Order[] }>("/orders?status=pending"),
      ]);
      if (userRef.current?.id !== owner || ordersRequest.current !== request)
        return;
      const fetchedStatuses = new Set<Order["status"]>();
      const incoming: Order[] = [];
      for (const [index, result] of results.entries()) {
        if (result.status !== "fulfilled") continue;
        fetchedStatuses.add(index === 0 ? "succeeded" : "pending");
        incoming.push(...result.value.items);
      }
      setOrders((previous) => {
        const merged = new Map<string, Order>();
        const cached = new Map(previous.map((order) => [order.id, order]));
        const incomingIds = new Set(incoming.map((order) => order.id));
        for (const order of previous) {
          if (
            !fetchedStatuses.has(order.status) ||
            (orderWriteVersions.current.get(order.id) ?? 0) > version ||
            (order.status === "pending" &&
              order.has_payment &&
              !incomingIds.has(order.id))
          )
            merged.set(order.id, order);
        }
        for (const order of incoming) {
          if ((orderWriteVersions.current.get(order.id) ?? 0) > version)
            continue;
          if (
            order.status === "pending" &&
            ((cached.has(order.id) &&
              cached.get(order.id)?.status !== "pending") ||
              merged.get(order.id)?.status === "succeeded")
          )
            continue;
          merged.set(order.id, order);
        }
        return [...merged.values()].sort((a, b) =>
          b.created_at.localeCompare(a.created_at),
        );
      });
      settleOrders(
        incoming.filter(
          (order) =>
            userRef.current?.id === owner &&
            ordersRequest.current === request &&
            (orderWriteVersions.current.get(order.id) ?? 0) <= version,
        ),
      );
      const paid = results[0];
      if (paid.status === "fulfilled") setOrdersReady(true);
      else setOrdersError(message(paid.reason));
    } finally {
      if (userRef.current?.id === owner && ordersRequest.current === request)
        setOrdersLoading(false);
    }
  }, [user, cartReady, settleOrders]);
  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [settings, catalog] = await Promise.all([
        api<Config>("/config"),
        api<{ items: AppInfo[] }>("/apps"),
      ]);
      setConfig(settings);
      setApps(catalog.items);
      setSelection((previous) => {
        if (!previous) return null;
        const available = previous
          .map((old) => catalog.items.find((app) => app.slug === old.slug))
          .filter((app): app is PurchasableApp =>
            Boolean(app && canPurchaseApp(app)),
          );
        return available.length ? available : null;
      });
    } catch (cause) {
      setError(message(cause));
    } finally {
      setLoading(false);
    }
  }, []);
  const toggleCart = useCallback(
    (slug: string) => {
      if (
        !cartReady ||
        !apps.some((app) => app.slug === slug && canPurchaseApp(app))
      )
        return;
      const selected = cartRef.current.includes(slug);
      updateCart(
        selected
          ? cartRef.current.filter((id) => id !== slug)
          : [...cartRef.current, slug],
      );
    },
    [apps, cartReady, updateCart],
  );
  const checkout = useCallback(
    (anchor?: HTMLElement) => {
      const selected = apps
        .filter(canPurchaseApp)
        .filter((app) => cartRef.current.includes(app.slug));
      if (!selected.length) return;
      checkoutAnchor.current =
        anchor ??
        (document.activeElement instanceof HTMLElement
          ? document.activeElement
          : null);
      setSelection(selected);
    },
    [apps],
  );
  const closeCheckout = useCallback(() => {
    setSelection(null);
    requestAnimationFrame(() => {
      const previous = checkoutAnchor.current;
      const fallback = document.querySelector<HTMLElement>(
        ".cart-summary .button",
      );
      const target =
        previous?.isConnected && previous.getClientRects().length
          ? previous
          : fallback;
      target?.focus({ preventScroll: true });
    });
  }, []);
  useEffect(() => {
    void load();
    void authenticate()
      .then(setUser)
      .catch((cause) => setAuthError(message(cause)))
      .finally(() => setAuthReady(true));
    const tg = window.Telegram?.WebApp;
    tg?.ready();
    tg?.expand();
    tg?.setHeaderColor?.("#FFFFFF");
    tg?.setBackgroundColor?.("#FFFFFF");
    const pop = () => {
      setSelection(null);
      setPath(window.location.pathname.replace(/\/$/, "") || "/");
    };
    const unauthorized = () => {
      setCartReady(false);
      setUser(null);
      setOrders([]);
      setOrdersReady(false);
      setSelection(null);
      setAuthError(
        "Сессия завершилась. Закройте и снова откройте мини-приложение из бота.",
      );
    };
    window.addEventListener("popstate", pop);
    window.addEventListener("appki:unauthorized", unauthorized);
    return () => {
      window.removeEventListener("popstate", pop);
      window.removeEventListener("appki:unauthorized", unauthorized);
    };
  }, [load]);
  useEffect(() => {
    setOrders([]);
    setOrdersReady(false);
    orderWriteVersions.current.clear();
    void refreshOrders();
  }, [refreshOrders]);
  const pendingPaymentIds = orders
    .filter(
      (order) =>
        order.status === "pending" &&
        order.has_payment &&
        !order.requires_payment_review &&
        order.test === (config?.payments_mode === "test"),
    )
    .map((order) => order.id)
    .sort()
    .join(",");
  useEffect(() => {
    if (!user || !pendingPaymentIds) return;
    const owner = user.id;
    const ids = pendingPaymentIds.split(",");
    let stopped = false;
    let busy = false;
    let cursor = 0;
    const reconcile = async () => {
      if (busy || document.visibilityState !== "visible") return;
      busy = true;
      const batch = ids.slice(cursor, cursor + 3);
      cursor = cursor + 3 >= ids.length ? 0 : cursor + 3;
      const versions = new Map(
        batch.map((id) => [id, orderWriteVersions.current.get(id) ?? 0]),
      );
      try {
        const results = await Promise.allSettled(
          batch.map((id) => api<Order>(`/orders/${encodeURIComponent(id)}`)),
        );
        if (!stopped && userRef.current?.id === owner)
          for (const result of results)
            if (
              result.status === "fulfilled" &&
              (orderWriteVersions.current.get(result.value.id) ?? 0) ===
                versions.get(result.value.id)
            )
              recordOrder(result.value);
      } finally {
        busy = false;
      }
    };
    void reconcile();
    const timer = window.setInterval(() => void reconcile(), 8000);
    const focus = () => void reconcile();
    document.addEventListener("visibilitychange", focus);
    window.addEventListener("focus", focus);
    return () => {
      stopped = true;
      clearInterval(timer);
      document.removeEventListener("visibilitychange", focus);
      window.removeEventListener("focus", focus);
    };
  }, [pendingPaymentIds, user, recordOrder]);
  useEffect(() => {
    if (!authReady || loading) return;
    cartStorageKey.current = `appki.cart.v1.${user?.id ?? "public"}`;
    let saved: unknown = [];
    try {
      saved = JSON.parse(
        sessionStorage.getItem(cartStorageKey.current) ?? "[]",
      );
    } catch {
      /* Start with an empty cart. */
    }
    const ids = Array.isArray(saved)
      ? [
          ...new Set(
            saved.filter(
              (id): id is string =>
                typeof id === "string" &&
                apps.some((app) => app.slug === id && canPurchaseApp(app)),
            ),
          ),
        ]
      : [];
    updateCart(ids);
    setCartReady(true);
  }, [authReady, user?.id, loading, apps, updateCart]);
  useEffect(() => {
    const scroll = main.current;
    if (!scroll || lastPath.current === path) return;
    if (lastPath.current === "/") catalogScroll.current = scroll.scrollTop;
    scroll.scrollTop = path === "/" ? catalogScroll.current : 0;
    lastPath.current = path;
  }, [path]);
  useEffect(() => {
    if (!announcement) return;
    const timer = window.setTimeout(() => setAnnouncement(""), 3500);
    return () => clearTimeout(timer);
  }, [announcement]);
  const keepFocusedControlVisible = useCallback(() => {
    const scroll = main.current;
    const active = document.activeElement;
    if (!scroll || !(active instanceof HTMLElement) || !scroll.contains(active))
      return;
    requestAnimationFrame(() => {
      const box = active.getBoundingClientRect();
      const viewport = scroll.getBoundingClientRect();
      const capsule = cartVisible
        ? scroll.closest(".app-content")?.querySelector(".cart-summary")
        : null;
      const bottom = Math.min(
        viewport.bottom,
        capsule?.getBoundingClientRect().top ?? viewport.bottom,
      );
      if (box.bottom > bottom - 12)
        scroll.scrollBy({
          top: box.bottom - bottom + 12,
          behavior: "instant",
        });
    });
  }, [cartVisible]);
  useEffect(() => {
    keepFocusedControlVisible();
  }, [cart.length, keepFocusedControlVisible]);
  return (
    <Context.Provider
      value={{
        config,
        apps,
        user,
        authReady,
        authError,
        path,
        navigate,
        toast: showToast,
        orders,
        ordersLoading,
        ordersReady,
        ordersError,
        refreshOrders,
        recordOrder,
        cart,
        cartReady,
        toggleCart,
        checkout,
      }}
    >
      <div className="app-shell" data-cart-visible={cartVisible}>
        <div className="app-content" inert={Boolean(selection)}>
          <Header />
          <main
            className="app-main"
            id="main"
            ref={main}
            onFocusCapture={keepFocusedControlVisible}
          >
            {loading ? (
              <Skeleton />
            ) : error ? (
              <>
                <Notice tone="error">{error}</Notice>
                <Button onClick={() => void load()}>Попробовать ещё раз</Button>
              </>
            ) : (
              <>
                <div hidden={path !== "/" && !path.startsWith("/apps/")}>
                  <Catalog />
                </div>
                <Route />
              </>
            )}
          </main>
          <footer className="app-footer">
            <CartSummary />
            <Navigation />
          </footer>
        </div>
        {selection && (
          <CheckoutDialog
            selection={selection}
            onClose={closeCheckout}
            reloadCatalog={load}
          />
        )}
        {announcement && (
          <div
            className={`toast ${announcementTone === "success" ? "toast-success" : ""}`}
            role="status"
          >
            <Icon name="check" size={18} />
            {announcement}
          </div>
        )}
      </div>
    </Context.Provider>
  );
}
