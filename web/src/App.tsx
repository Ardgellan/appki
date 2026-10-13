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
  api,
  authenticate,
  clearPurchaseRequest,
  purchaseRequestId,
} from "./api";
import {
  appCount,
  cartAmount,
  cartRequestKey,
  orderItems,
  purchasedApps,
  routeForItem,
} from "./checkout";
import { AppIcon, BrandMark, Icon } from "./icons";
import type {
  Activation,
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
  toast: (message: string) => void;
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
  const selected = apps.filter(
    (app) => cart.includes(app.slug) && app.available,
  );
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
  const {
    apps,
    cart,
    cartReady,
    orders,
    config,
    toggleCart,
    checkout,
    navigate,
    path,
  } = useApp();
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState("Все");
  const [expanded, setExpanded] = useState<string | null>(null);
  useEffect(() => {
    if (path.startsWith("/apps/")) setExpanded(routePart(path.slice(6)));
  }, [path]);
  const owned = purchasedApps(orders, config?.payments_mode ?? "test");
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
          const purchase = owned.find(
            (entry) => entry.item.app_slug === app.slug,
          );
          const open = expanded === app.slug;
          return (
            <article className="app-card" key={app.slug} data-expanded={open}>
              <div className="app-card-row">
                <AppIcon slug={app.slug} title={app.title} />
                <div className="app-card-copy">
                  <strong>{app.title}</strong>
                  <span>
                    {app.category === "VPN" ? "VPN-клиент" : app.subtitle}
                  </span>
                </div>
                <div className="card-actions">
                  <button
                    type="button"
                    className="cart-toggle"
                    aria-label={
                      purchase
                        ? `${app.title} уже приобретено`
                        : `${selected ? "Убрать" : "Добавить"} ${app.title} ${selected ? "из корзины" : "в корзину"}`
                    }
                    aria-pressed={selected}
                    data-owned={Boolean(purchase)}
                    disabled={!cartReady || !app.available || Boolean(purchase)}
                    onClick={() => toggleCart(app.slug)}
                  >
                    <Icon name="basket" size={10.5} />
                  </button>
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
                    <Specs app={app} />
                    <div className="card-price">
                      <span>Доступ к установке</span>
                      <strong>{rubles(app.price_rub)}</strong>
                    </div>
                    <button
                      type="button"
                      className="button card-cta"
                      disabled={(!app.available || !cartReady) && !purchase}
                      onClick={(event) =>
                        purchase
                          ? navigate(routeForItem(purchase.order.id, app.slug))
                          : selected
                            ? checkout(event.currentTarget)
                            : toggleCart(app.slug)
                      }
                    >
                      {purchase
                        ? "Открыть в моих приложениях"
                        : !app.available
                          ? "Пока недоступно"
                          : selected
                            ? "Перейти к оплате"
                            : "Добавить в корзину"}
                      <Icon
                        name={selected || purchase ? "arrow" : "basket"}
                        size={selected || purchase ? 17 : 10.5}
                      />
                    </button>
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

function CheckoutDialog({
  selection,
  onClose,
  reloadCatalog,
}: {
  selection: AppInfo[];
  onClose: () => void;
  reloadCatalog: () => Promise<void>;
}) {
  const {
    config,
    user,
    orders,
    ordersReady,
    ordersError,
    refreshOrders,
    recordOrder,
    navigate,
    toast,
  } = useApp();
  const alreadyOwned = selection.some((app) =>
    purchasedApps(orders, config?.payments_mode ?? "test").some(
      (entry) => entry.item.app_slug === app.slug,
    ),
  );
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const sheet = useRef<HTMLElement>(null);
  const close = useRef<HTMLButtonElement>(null);
  const locked = useRef(false);
  useEffect(() => {
    close.current?.focus();
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
      alreadyOwned ||
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
    try {
      const order = await api<Order>("/orders", {
        method: "POST",
        body: JSON.stringify({
          app_slugs: slugs,
          expected_amount_rub: cartAmount(selection),
          request_id: purchaseRequestId(
            cartRequestKey(slugs, config.payments_mode),
          ),
        }),
      });
      recordOrder(order);
      onClose();
      if (order.status === "succeeded") {
        navigate("/orders");
        toast("Оплата подтверждена. Приложения доступны для установки.");
      } else {
        navigate(`/payment/${encodeURIComponent(order.id)}`);
        if (order.payment_url) external(order.payment_url);
      }
    } catch (cause) {
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
          <div className="instruction-copy">
            <section>
              <h3>1. Установка приложения</h3>
              <p>
                После оплаты откройте «Мои приложения» и выберите приложение.
                Получите персональную ссылку, откройте её в Safari на iPhone и
                подтвердите установку. Ссылка действует ограниченное время —
                новую можно получить в том же заказе.
              </p>
            </section>
            <section>
              <h3>2. Активация</h3>
              <p>
                Для первого запуска может понадобиться авторизация установочного
                Apple Account. Инструкция и помощь доступны в оплаченном заказе.
                Вход выполняется только в App Store или «Контент и покупки», без
                выхода из основного iCloud.
              </p>
            </section>
            <section>
              <h3>3. Первый запуск</h3>
              <p>
                После успешного запуска вернитесь в свой аккаунт App Store.
                Установка не переносит приложение в покупки вашего Apple
                Account. Подписки сторонних сервисов и VPN-серверы приобретаются
                отдельно. Перед переустановкой сохраните настройки существующей
                копии.
              </p>
            </section>
          </div>
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
            {config?.payments_mode === "test" && (
              <p className="test-note">
                Тестовая оплата ЮKassa. Реальные деньги не списываются.
              </p>
            )}
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
            {alreadyOwned && (
              <Notice>Приложение уже доступно в «Моих приложениях».</Notice>
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
                alreadyOwned ||
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
            <p className="muted small-text checkout-method">
              {config?.payments_mode === "test"
                ? "Тестовый платёж"
                : "Через СБП"}
            </p>
          </div>
        </div>
      </section>
    </div>
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
    recordOrder,
  } = useApp();
  useEffect(() => {
    void refreshOrders();
  }, [refreshOrders]);
  const owned = purchasedApps(orders, config?.payments_mode ?? "test");
  const pending = orders.filter(
    (order) =>
      order.status === "pending" &&
      order.test === (config?.payments_mode === "test"),
  );
  const pendingIds = pending.map((order) => order.id).join(",");
  useEffect(() => {
    if (!user || !pendingIds) return;
    let stopped = false;
    let busy = false;
    const reconcile = async () => {
      if (busy || document.visibilityState !== "visible") return;
      busy = true;
      const results = await Promise.allSettled(
        pendingIds
          .split(",")
          .slice(0, 3)
          .map((id) => api<Order>(`/orders/${encodeURIComponent(id)}`)),
      );
      if (!stopped)
        for (const result of results)
          if (result.status === "fulfilled") recordOrder(result.value);
      busy = false;
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
  }, [pendingIds, user, recordOrder]);
  return (
    <section>
      <div className="page-heading">
        <h1>Мои приложения</h1>
        <p>Ваши приложения и доступ к установке.</p>
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
              <button
                type="button"
                className="order-row"
                key={item.app_slug}
                aria-label={`Открыть ${item.app_title}`}
                onClick={() => navigate(routeForItem(order.id, item.app_slug))}
              >
                <AppIcon slug={item.app_slug} title={item.app_title} />
                <div className="order-row-info">
                  <strong>{item.app_title}</strong>
                  <span>
                    {item.activated
                      ? "Активировано"
                      : item.installed
                        ? "Нужна активация"
                        : "Установить и активировать"}
                  </span>
                </div>
                <Icon name="chevronRight" size={18} />
              </button>
            ))}
          </div>
          {!owned.length && !ordersError && (
            <div className="empty-state">
              <p>Здесь появятся приобретённые приложения.</p>
              <Button onClick={() => navigate("/")}>
                Выбрать приложения <Icon name="arrow" size={18} />
              </Button>
            </div>
          )}
          {!!pending.length && (
            <>
              <h2 className="history-heading">Ожидают оплаты</h2>
              <div className="orders-list">
                {pending.map((order) => (
                  <button
                    type="button"
                    className="order-row"
                    key={order.id}
                    onClick={() =>
                      navigate(`/payment/${encodeURIComponent(order.id)}`)
                    }
                  >
                    <Icon name="bank" size={22} />
                    <div className="order-row-info">
                      <strong>{appCount(orderItems(order).length)}</strong>
                      <span>
                        {rubles(order.amount_rub)} ·{" "}
                        {formatDate(order.created_at)}
                      </span>
                    </div>
                    <Icon name="chevronRight" size={18} />
                  </button>
                ))}
              </div>
            </>
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
function ActivationPanel({
  order,
  item,
  onComplete,
}: {
  order: Order;
  item: OrderItem;
  onComplete: () => Promise<void>;
}) {
  const { config } = useApp();
  const [data, setData] = useState<Activation | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const load = useCallback(async () => {
    setBusy(true);
    setError("");
    try {
      setData(
        await api<Activation>(
          `/orders/${encodeURIComponent(order.id)}/items/${encodeURIComponent(item.app_slug)}/activation`,
        ),
      );
    } catch (cause) {
      setError(message(cause));
    } finally {
      setBusy(false);
    }
  }, [order.id, item.app_slug]);
  useEffect(() => {
    void load();
  }, [load]);
  const support = supportURL(data?.support_url || config?.support_url);
  async function complete() {
    setBusy(true);
    setError("");
    try {
      await onComplete();
    } catch (cause) {
      setError(message(cause));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="surface activation-panel">
      <h2>Активируйте приложение</h2>
      {busy && !data ? <Skeleton /> : null}
      {error && (
        <>
          <Notice tone="error">{error}</Notice>
          {!data && (
            <Button secondary onClick={load}>
              Повторить
            </Button>
          )}
        </>
      )}
      {data && (
        <>
          <p className="preserve-lines">{data.instructions}</p>
          {data.mode === "credentials" && (
            <>
              {data.apple_id && (
                <SecretField label="Apple Account" value={data.apple_id} />
              )}
              {data.password && (
                <SecretField label="Пароль" value={data.password} password />
              )}
              <Notice>
                Входите только в App Store («Контент и покупки»). Основной
                iCloud менять не нужно. Код подтверждения вводите только в
                системном окне Apple.
              </Notice>
            </>
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
          ) : data.mode === "manual" ? (
            <p className="muted small-text">
              Канал поддержки ещё настраивается.
            </p>
          ) : null}
          <Button className="full" busy={busy} onClick={complete}>
            Приложение запустилось <Icon name="check" size={18} />
          </Button>
          <p className="muted small-text">
            Нажмите после успешного запуска. Этот шаг сохраняет вашу отметку о
            завершении.
          </p>
        </>
      )}
    </div>
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
  const { user, authReady, recordOrder, navigate, toast } = useApp();
  const [order, setOrder] = useState<Order | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [checking, setChecking] = useState(false);
  const [grant, setGrant] = useState<Grant | null>(null);
  const [grantBusy, setGrantBusy] = useState(false);
  const [grantError, setGrantError] = useState("");
  const [step, setStep] = useState<"install" | "activate">("install");
  const [showInstructions, setShowInstructions] = useState(false);
  const initialized = useRef(false);
  const loadingPayment = useRef(false);
  const load = useCallback(
    async (quiet = false) => {
      if (!user || !id || loadingPayment.current) {
        if (!user || !id) setLoading(false);
        return;
      }
      loadingPayment.current = true;
      if (!quiet) setChecking(true);
      try {
        const fresh = await api<Order>(`/orders/${encodeURIComponent(id)}`);
        setOrder(fresh);
        recordOrder(fresh);
        setError("");
        if ((paymentReturn || paymentPage) && fresh.status === "succeeded") {
          navigate("/orders");
          toast("Оплата подтверждена. Можно устанавливать приложения.");
        }
        if (!initialized.current) {
          const selected =
            orderItems(fresh).find((item) => item.app_slug === slug) ??
            orderItems(fresh)[0];
          setStep(selected.installed ? "activate" : "install");
          initialized.current = true;
        }
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
    if (order?.status !== "pending") return;
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
  }, [order?.status, load]);
  async function retryPayment() {
    if (!order || checking) return;
    setChecking(true);
    setError("");
    try {
      const next = await api<Order>(
        `/orders/${encodeURIComponent(order.id)}/retry-payment`,
        { method: "POST" },
      );
      setOrder(next);
      recordOrder(next);
      if (next.payment_url) external(next.payment_url);
    } catch (cause) {
      setError(message(cause));
    } finally {
      setChecking(false);
    }
  }
  const items = order ? orderItems(order) : [];
  const selected =
    items.find((item) => item.app_slug === slug) ??
    (items.length === 1 ? items[0] : null);
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
    setOrder(changed);
    recordOrder(changed);
    if (update.installed) setStep("activate");
    if (update.activated) {
      setShowInstructions(false);
      toast(`${next.app_title} — активация завершена.`);
    }
  }
  async function installLink() {
    if (!order || !selected || grantBusy) return;
    setGrantBusy(true);
    setGrantError("");
    try {
      const next = await api<Grant>(
        `/orders/${encodeURIComponent(order.id)}/items/${encodeURIComponent(selected.app_slug)}/install-link`,
        { method: "POST" },
      );
      const url = httpsURL(next.url);
      if (!url || new URL(url).origin !== window.location.origin)
        throw new Error(
          "Сервер вернул некорректную ссылку установки. Обратитесь в поддержку.",
        );
      setGrant(next);
      external(url);
    } catch (cause) {
      setGrantError(message(cause));
    } finally {
      setGrantBusy(false);
    }
  }
  async function installed() {
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
    if (!grant) return;
    try {
      await navigator.clipboard.writeText(grant.url);
      toast("Ссылка скопирована. Откройте её в Safari на iPhone.");
    } catch {
      toast("Не удалось скопировать. Нажмите «Открыть установку».");
    }
  }
  if (!authReady || (user && loading))
    return (
      <>
        <Back label="Мои приложения" to="/orders" />
        <Skeleton />
      </>
    );
  if (!user)
    return (
      <section>
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
        <Back label="Мои приложения" to="/orders" />
        <Notice tone="error">{error || "Заказ не найден."}</Notice>
        <Button secondary busy={checking} onClick={() => void load()}>
          Проверить ещё раз
        </Button>
      </>
    );
  const paymentURL = httpsURL(order.payment_url);
  if (order.status !== "succeeded")
    return (
      <section>
        <Back label="Мои приложения" to="/orders" />
        <div className="payment-method">
          <Icon name="bank" size={19} />
          <span>
            {order.test ? "Тестовая оплата ЮKassa" : "Система быстрых платежей"}
          </span>
        </div>
        <div className="page-heading">
          <h1>
            {order.status === "pending"
              ? order.test
                ? "Тестовая оплата"
                : "Оплата через СБП"
              : orderStatus(order.status)}
          </h1>
        </div>
        <div className="payment-amount">{rubles(order.amount_rub)}</div>
        <p className="muted">
          Доступ к {items.map((item) => item.app_title).join(", ")}
        </p>
        {error && <Notice tone="error">{error}</Notice>}
        {order.test && (
          <p className="test-note">Реальные деньги не списываются.</p>
        )}
        {order.status === "pending" ? (
          <div className="surface payment-panel">
            <h2>
              {paymentReturn
                ? "Проверяем результат оплаты"
                : order.test
                  ? "Оплата на тестовой странице"
                  : "Оплатите в приложении банка"}
            </h2>
            <p>
              {order.test
                ? "Платёж откроется на тестовой странице ЮKassa."
                : "Подтвердите платёж через СБП."}{" "}
              После подтверждения сервером приложения появятся в «Моих
              приложениях».
            </p>
            {paymentURL ? (
              <a
                className="button full"
                href={paymentURL}
                target="_blank"
                rel="noreferrer"
                onClick={(event) => {
                  if (window.Telegram?.WebApp?.initData) {
                    event.preventDefault();
                    external(paymentURL);
                  }
                }}
              >
                Открыть {order.test ? "тестовую оплату" : "оплату через СБП"}
                <Icon name="external" size={17} />
              </a>
            ) : (
              <>
                <Notice>
                  Создание платежа не завершилось. Повторим запрос для того же
                  заказа.
                </Notice>
                <Button className="full" busy={checking} onClick={retryPayment}>
                  Повторить создание оплаты
                </Button>
              </>
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
                ? "Если есть вопросы, обратитесь в поддержку по номеру заказа."
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
        <p className="order-reference">Заказ {order.id}</p>
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
            <button
              type="button"
              className="order-row"
              key={item.app_slug}
              onClick={() => navigate(routeForItem(order.id, item.app_slug))}
            >
              <AppIcon slug={item.app_slug} title={item.app_title} />
              <div className="order-row-info">
                <strong>{item.app_title}</strong>
                <span>
                  {item.activated
                    ? "Активировано"
                    : item.installed
                      ? "Нужна активация"
                      : "Установить и активировать"}
                </span>
              </div>
              <Icon name="chevronRight" size={18} />
            </button>
          ))}
        </div>
      </section>
    );
  const hasValidGrant = grant && Date.parse(grant.expires_at) > Date.now();
  return (
    <section>
      <Back label="Мои приложения" to="/orders" />
      <div className="flow-heading">
        <AppIcon slug={selected.app_slug} title={selected.app_title} large />
        <div>
          <h1>{selected.app_title}</h1>
          <p>
            {selected.activated
              ? "Активировано"
              : selected.installed
                ? "Приложение установлено"
                : "Доступ приобретён"}
          </p>
        </div>
      </div>
      {error && <Notice tone="error">{error}</Notice>}
      {order.test && <p className="test-note">Тестовый заказ.</p>}
      {!selected.can_install ? (
        <Notice>
          Платёж подтверждён, но установка пока недоступна. Обратитесь в
          поддержку.
        </Notice>
      ) : selected.activated && !showInstructions ? (
        <div className="surface">
          <Icon name="check" size={28} />
          <h2>Всё готово</h2>
          <p>Вы отметили приложение установленным и активированным.</p>
          <Button className="full" onClick={() => navigate("/orders")}>
            Мои приложения <Icon name="arrow" size={18} />
          </Button>
          <Button
            className="full"
            secondary
            onClick={() => {
              setStep("install");
              setShowInstructions(true);
            }}
          >
            Инструкция по переустановке
          </Button>
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
              aria-current={step === "install" ? "step" : undefined}
              data-done={selected.installed}
              onClick={() => setStep("install")}
            >
              <span>1</span>Установка
            </button>
            <button
              type="button"
              disabled={!selected.installed}
              aria-current={step === "activate" ? "step" : undefined}
              onClick={() => setStep("activate")}
            >
              <span>2</span>Активация
            </button>
          </div>
          {step === "install" ? (
            <div className="surface installation-panel">
              <h2>Установите приложение</h2>
              <p>
                Откройте персональную ссылку в Safari на iPhone и подтвердите
                системный запрос установки.
              </p>
              <p className="muted small-text">
                Перед переустановкой сохраните настройки существующей копии
                приложения.
              </p>
              <Button className="full" busy={grantBusy} onClick={installLink}>
                {hasValidGrant
                  ? "Получить новую ссылку"
                  : "Установить приложение"}
                <Icon name="arrow" size={17} />
              </Button>
              {hasValidGrant && (
                <div className="grant">
                  <Button
                    className="full"
                    secondary
                    onClick={() => external(grant.url)}
                  >
                    Открыть установку <Icon name="external" size={17} />
                  </Button>
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
              {(grant || selected.installed) && (
                <Button
                  className="full"
                  secondary
                  busy={grantBusy}
                  onClick={installed}
                >
                  {selected.installed
                    ? "Перейти к активации"
                    : "Приложение установлено"}
                  <Icon name="check" size={18} />
                </Button>
              )}
            </div>
          ) : (
            <ActivationPanel
              key={`${order.id}:${selected.app_slug}`}
              order={order}
              item={selected}
              onComplete={() => markState({ activated: true })}
            />
          )}
        </>
      )}
      <p className="order-reference">Заказ {order.id}</p>
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
    "Достаточно iPhone и Safari. Персональная установочная ссылка действует ограниченное время. Новую ссылку можно получить в оплаченном заказе.",
  ],
  [
    "Почему приложение не открывается?",
    "После установки может понадобиться активация. Откройте приложение в «Моих приложениях», перейдите к шагу «Активация» и следуйте инструкции. Если нужна помощь, сообщите поддержке номер заказа, версию iOS и текст ошибки.",
  ],
  [
    "Нужно ли выходить из iCloud?",
    "Нет. Если требуется авторизация, она выполняется только в App Store или «Контент и покупки». Основной аккаунт iCloud и «Локатор» менять не нужно. Никогда не вводите свой пароль Apple на странице сервиса.",
  ],
  [
    "Можно ли вернуться в свой аккаунт?",
    "В нашем тесте Happ продолжил работать после возврата в собственный аккаунт App Store. Совместимость зависит от версии iOS и приложения. Обновление или повторная установка могут потребовать повторную авторизацию.",
  ],
  [
    "Что делать, если приложение уже установлено?",
    "Сначала сохраните его настройки. Система может потребовать удалить установленную копию; локальные данные при этом могут потеряться. Для VPN сохраните ссылку подписки и запасной способ доступа в интернет.",
  ],
  [
    "Как устроена оплата и возврат?",
    "Платёж обрабатывает ЮKassa. Доступ выдаётся после подтверждения сервером. В тестовом режиме реальные деньги не списываются. Если установка не удаётся или нужен возврат, обратитесь в поддержку с номером заказа.",
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
        <p>Напишите, на каком шаге возник вопрос. Добавьте номер заказа.</p>
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
          Аппки — экспериментальный сервис доставки приложений. Совместимость
          зависит от устройства, версии iOS и доступности авторизации Apple. Мы
          не связаны с Apple или разработчиками приложений.
        </p>
        <p>
          Не передавайте другим людям установочные ссылки и данные активации. Не
          отправляйте поддержке пароли и коды личного аккаунта. Условия
          коммерческой продажи и информация о продавце будут опубликованы до
          запуска реальных платежей.
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
  const [orders, setOrders] = useState<Order[]>([]);
  const [ordersLoading, setOrdersLoading] = useState(false);
  const [ordersReady, setOrdersReady] = useState(false);
  const [ordersError, setOrdersError] = useState("");
  const [cart, setCart] = useState<string[]>([]);
  const [cartReady, setCartReady] = useState(false);
  const cartRef = useRef<string[]>([]);
  const cartStorageKey = useRef("");
  const [selection, setSelection] = useState<AppInfo[] | null>(null);
  const checkoutAnchor = useRef<HTMLElement | null>(null);
  const main = useRef<HTMLElement>(null);
  const catalogScroll = useRef(0);
  const lastPath = useRef(path);
  const userRef = useRef<User | null>(null);
  const orderWrites = useRef(0);
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
  const recordOrder = useCallback(
    (order: Order) => {
      orderWrites.current++;
      setOrders((previous) =>
        [order, ...previous.filter((item) => item.id !== order.id)].sort(
          (a, b) => b.created_at.localeCompare(a.created_at),
        ),
      );
      if (order.status !== "pending")
        clearPurchaseRequest(
          cartRequestKey(
            orderItems(order).map((item) => item.app_slug),
            order.test ? "test" : "live",
          ),
        );
      if (order.status === "succeeded") {
        const purchased = orderItems(order).map((item) => item.app_slug);
        updateCart(cartRef.current.filter((slug) => !purchased.includes(slug)));
      }
    },
    [updateCart],
  );
  const refreshOrders = useCallback(async () => {
    if (!user) return;
    const owner = user.id;
    const version = orderWrites.current;
    setOrdersLoading(true);
    setOrdersError("");
    try {
      const result = await api<{ items: Order[] }>("/orders");
      if (userRef.current?.id === owner) {
        if (orderWrites.current === version) setOrders(result.items);
        setOrdersReady(true);
      }
    } catch (cause) {
      if (userRef.current?.id === owner) setOrdersError(message(cause));
    } finally {
      if (userRef.current?.id === owner) setOrdersLoading(false);
    }
  }, [user]);
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
          .filter((app): app is AppInfo => Boolean(app?.available));
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
        !apps.some((app) => app.slug === slug && app.available) ||
        purchasedApps(orders, config?.payments_mode ?? "test").some(
          (entry) => entry.item.app_slug === slug,
        )
      )
        return;
      const selected = cartRef.current.includes(slug);
      updateCart(
        selected
          ? cartRef.current.filter((id) => id !== slug)
          : [...cartRef.current, slug],
      );
    },
    [apps, cartReady, orders, config?.payments_mode, updateCart],
  );
  const checkout = useCallback(
    (anchor?: HTMLElement) => {
      const selected = apps.filter(
        (app) => cartRef.current.includes(app.slug) && app.available,
      );
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
    void refreshOrders();
  }, [refreshOrders]);
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
                apps.some((app) => app.slug === id && app.available),
            ),
          ),
        ]
      : [];
    updateCart(ids);
    setCartReady(true);
  }, [authReady, user?.id, loading, apps, updateCart]);
  useEffect(() => {
    const owned = purchasedApps(orders, config?.payments_mode ?? "test").map(
      (entry) => entry.item.app_slug,
    );
    const next = cartRef.current.filter((slug) => !owned.includes(slug));
    if (next.length !== cartRef.current.length) updateCart(next);
    setSelection((previous) => {
      if (!previous || !previous.some((app) => owned.includes(app.slug)))
        return previous;
      const remaining = previous.filter((app) => !owned.includes(app.slug));
      return remaining.length ? remaining : null;
    });
  }, [orders, config?.payments_mode, updateCart]);
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
  useEffect(() => {
    const scroll = main.current;
    const active = document.activeElement;
    if (!scroll || !(active instanceof HTMLElement) || !scroll.contains(active))
      return;
    requestAnimationFrame(() => {
      const box = active.getBoundingClientRect();
      const viewport = scroll.getBoundingClientRect();
      if (box.bottom > viewport.bottom - 12)
        scroll.scrollBy({
          top: box.bottom - viewport.bottom + 12,
          behavior: "instant",
        });
    });
  }, [cart.length]);
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
        toast: setAnnouncement,
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
      <div className="app-shell">
        <div className="app-content" inert={Boolean(selection)}>
          <Header />
          <main className="app-main" id="main" ref={main}>
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
          <div className="toast" role="status">
            <Icon name="check" size={18} />
            {announcement}
          </div>
        )}
      </div>
    </Context.Provider>
  );
}
