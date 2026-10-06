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
import { AppIcon, BrandMark, Icon } from "./icons";
import type { Activation, AppInfo, Config, Grant, Order, User } from "./types";
import {
  fileSize,
  formatDate,
  formatTime,
  httpsURL,
  orderStatus,
  routeForOrder,
  rubles,
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
}
const Context = createContext<AppState>(null!);
const useApp = () => useContext(Context);
function message(error: unknown) {
  return error instanceof Error
    ? error.message
    : "Не получилось. Попробуйте ещё раз.";
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
  type = "button",
}: {
  children: ReactNode;
  onClick?: () => void;
  disabled?: boolean;
  secondary?: boolean;
  busy?: boolean;
  className?: string;
  type?: "button" | "submit";
}) {
  return (
    <button
      type={type}
      className={`button ${secondary ? "secondary" : ""} ${className}`}
      onClick={onClick}
      disabled={disabled || busy}
    >
      {busy ? <span className="spinner" /> : null}
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
      role={tone === "error" ? "alert" : undefined}
    >
      <Icon
        name={
          tone === "success" ? "check" : tone === "error" ? "help" : "shield"
        }
        size={20}
      />
      <div>{children}</div>
    </div>
  );
}
function Skeleton() {
  return (
    <div className="skeleton-wrap" role="status" aria-label="Загрузка">
      <div className="skeleton line" />
      <div className="skeleton block" />
      <div className="skeleton block small" />
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
    <button className="back" onClick={() => navigate(to)}>
      <Icon name="back" size={18} />
      {label}
    </button>
  );
}
function TestBadge() {
  return (
    <span className="test-badge">
      <span />
      Тестовая оплата
    </span>
  );
}
function TelegramGate() {
  const { config, authReady, authError } = useApp();
  const link = telegramURL(config?.bot_username);
  if (!authReady) return <Notice>Подключаем ваш профиль Telegram…</Notice>;
  return (
    <div className="telegram-gate">
      <div className="gate-icon">
        <Icon name="telegram" size={24} />
      </div>
      <div>
        <strong>Продолжите в Telegram</strong>
        <p>Там сохранятся покупки, ссылки на установку и инструкции.</p>
        {authError && <p className="error-text">{authError}</p>}
      </div>
      {link ? (
        <a className="button" href={link} rel="noreferrer">
          Открыть бота
          <Icon name="arrow" size={18} />
        </a>
      ) : (
        <p className="muted small-text">
          Бот подключается. Покупки пока недоступны.
        </p>
      )}
    </div>
  );
}
function Header() {
  const { config, navigate, user } = useApp();
  return (
    <header className="header">
      <button
        className="brand"
        onClick={() => navigate("/")}
        aria-label="Аппки — на главную"
      >
        <BrandMark />
        <span>
          {config?.brand || "Аппки"}
          <span className="brand-dot">.</span>
        </span>
      </button>
      <div className="header-right">
        <span className="platform-label">
          <Icon name="phone" size={15} />
          Для iPhone
        </span>
        {user && (
          <span
            className="avatar"
            title={user.first_name}
            aria-label={`Профиль: ${user.first_name}`}
          >
            {user.first_name.slice(0, 1).toUpperCase()}
          </span>
        )}
      </div>
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
          active: path === "/" || path.startsWith("/apps/"),
        },
        {
          route: "/orders",
          icon: "stack",
          text: "Мои приложения",
          active: path.startsWith("/orders") || path.startsWith("/payment"),
        },
        {
          route: "/help",
          icon: "help",
          text: "Помощь",
          active: path === "/help",
        },
      ].map((item) => (
        <button
          key={item.route}
          className={item.active ? "active" : ""}
          onClick={() => navigate(item.route)}
          aria-current={item.active ? "page" : undefined}
        >
          <Icon name={item.icon} size={21} />
          <span>{item.text}</span>
        </button>
      ))}
    </nav>
  );
}
function Catalog() {
  const { apps, config, navigate } = useApp();
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState("Все");
  const categories = ["Все", ...new Set(apps.map((app) => app.category))];
  const filtered = apps.filter(
    (app) =>
      (category === "Все" || app.category === category) &&
      `${app.title} ${app.subtitle}`
        .toLowerCase()
        .includes(query.toLowerCase()),
  );
  return (
    <>
      <section className="hero">
        <div className="hero-copy">
          <div className="eyebrow">
            <span className="tiny-star">✳</span> ПРИЛОЖЕНИЯ СНОВА РЯДОМ
          </div>
          <h1>
            Нужное приложение.
            <br />
            <span>Снова на iPhone.</span>
          </h1>
          <p>
            Выберите приложение, получите доступ
            <br className="desktop-break" /> и установите по понятной
            инструкции.
          </p>
          <button className="text-link" onClick={() => navigate("/help")}>
            Как это работает
            <Icon name="arrow" size={18} />
          </button>
        </div>
        <div className="hero-art" aria-hidden="true">
          <div className="orb orb-one" />
          <div className="orb orb-two" />
          <div className="app-preview">
            <div className="preview-top">
              <span className="preview-brand">
                <BrandMark />
                Аппки
              </span>
              <span className="preview-ellipse">•••</span>
            </div>
            <div className="preview-app">
              <AppIcon />
              <div>
                <strong>Happ</strong>
                <span>Proxy Utility</span>
              </div>
              <div className="mini-check">
                <Icon name="check" size={16} />
              </div>
            </div>
            <div className="preview-line" />
            <div className="preview-bottom">
              <Icon name="phone" size={18} />
              <span>Ваш iPhone. Ваши приложения.</span>
            </div>
          </div>
          <div className="art-note">
            <Icon name="download" size={17} />
            <span>Установка по ссылке</span>
          </div>
        </div>
      </section>
      <section className="catalog-section">
        <div className="section-heading">
          <div>
            <span className="eyebrow muted">ВЫБИРАЙТЕ НУЖНОЕ</span>
            <h2>Каталог приложений</h2>
          </div>
          {config?.payments_mode === "test" && <TestBadge />}
        </div>
        <div className="catalog-tools">
          <div className="chips" aria-label="Категории">
            {categories.map((item) => (
              <button
                key={item}
                className={`chip ${category === item ? "selected" : ""}`}
                onClick={() => setCategory(item)}
              >
                {item === "Все" ? "Все приложения" : item}
                {item === "Все" && <span>{apps.length}</span>}
              </button>
            ))}
          </div>
          <label className="search">
            <Icon name="search" size={18} />
            <input
              aria-label="Поиск приложений"
              placeholder="Найти приложение"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
            {query && (
              <button aria-label="Очистить поиск" onClick={() => setQuery("")}>
                <Icon name="close" size={16} />
              </button>
            )}
          </label>
        </div>
        {filtered.length > 0 ? (
          <div className="app-grid">
            {filtered.map((app) => (
              <button
                className="app-card"
                key={app.slug}
                onClick={() =>
                  navigate(`/apps/${encodeURIComponent(app.slug)}`)
                }
              >
                <div className="card-top">
                  <AppIcon />
                  <span className="category-label">{app.category}</span>
                </div>
                <h3>{app.title}</h3>
                <p className="card-subtitle">{app.subtitle}</p>
                <p className="card-description">{app.description}</p>
                <div className="card-specs">
                  <span>iOS {app.minimum_ios}+</span>
                  <span className="dot-separator" />
                  <span>{fileSize(app.size_bytes)}</span>
                </div>
                <div className="card-bottom">
                  <div>
                    <span className="price">{rubles(app.price_rub)}</span>
                    <span className="price-caption">
                      {config?.payments_mode === "test"
                        ? "тестовая цена"
                        : "доступ к установке"}
                    </span>
                  </div>
                  <span
                    className={`card-action ${!app.available ? "unavailable" : ""}`}
                  >
                    {app.available ? "Подробнее" : "Скоро"}
                    <Icon name="arrow" size={18} />
                  </span>
                </div>
              </button>
            ))}
          </div>
        ) : (
          <div className="empty-state">
            <Icon name="search" size={30} />
            <h3>Ничего не нашлось</h3>
            <p>Попробуйте другое название или категорию.</p>
            <Button
              secondary
              onClick={() => {
                setQuery("");
                setCategory("Все");
              }}
            >
              Сбросить поиск
            </Button>
          </div>
        )}
        <div className="catalog-note">
          <Icon name="help" size={19} />
          <span>
            Перед покупкой посмотрите шаги установки. Для первого запуска может
            понадобиться авторизация в App Store.
          </span>
        </div>
      </section>
      <section className="how-strip">
        <div>
          <span className="step-index">01</span>
          <strong>Выберите приложение</strong>
          <p>Посмотрите описание и требования.</p>
        </div>
        <div>
          <span className="step-index">02</span>
          <strong>Получите доступ</strong>
          <p>Оплатите заказ через ЮKassa.</p>
        </div>
        <div>
          <span className="step-index">03</span>
          <strong>Установите на iPhone</strong>
          <p>Откройте ссылку и пройдите активацию.</p>
        </div>
      </section>
    </>
  );
}
function Detail({ slug }: { slug: string }) {
  const { apps, config, user, navigate } = useApp();
  const app = apps.find((item) => item.slug === slug);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const requestId = useRef<string | null>(null);
  async function checkout() {
    if (!app || busy) return;
    setBusy(true);
    setError("");
    try {
      requestId.current ??= purchaseRequestId(app.slug);
      const order = await api<Order>("/orders", {
        method: "POST",
        body: JSON.stringify({
          app_slug: app.slug,
          request_id: requestId.current,
        }),
      });
      navigate(routeForOrder(order.id));
    } catch (error) {
      setError(message(error));
    } finally {
      setBusy(false);
    }
  }
  if (!app)
    return (
      <>
        <Back />
        <div className="empty-state">
          <h1>Приложение не найдено</h1>
          <p>Вернитесь в каталог и выберите доступное приложение.</p>
        </div>
      </>
    );
  return (
    <>
      <Back />
      <div className="detail-layout">
        <section className="detail-main">
          <div className="app-heading">
            <AppIcon large />
            <div>
              <span className="category-label">{app.category}</span>
              <h1>{app.title}</h1>
              <p>{app.subtitle}</p>
            </div>
          </div>
          <div className="spec-grid">
            <div>
              <span>Версия</span>
              <strong>{app.version}</strong>
            </div>
            <div>
              <span>Размер</span>
              <strong>{fileSize(app.size_bytes)}</strong>
            </div>
            <div>
              <span>Совместимость</span>
              <strong>iOS {app.minimum_ios}+</strong>
            </div>
          </div>
          <div className="text-section">
            <h2>О приложении</h2>
            <p>{app.description}</p>
            {app.features.length > 0 && (
              <ul className="feature-list">
                {app.features.map((feature) => (
                  <li key={feature}>
                    <span>
                      <Icon name="check" size={15} />
                    </span>
                    {feature}
                  </li>
                ))}
              </ul>
            )}
          </div>
          <div className="text-section">
            <h2>Как проходит установка</h2>
            <ol className="step-list">
              <li>
                <span>1</span>
                <div>
                  <strong>Оплатите доступ</strong>
                  <p>
                    Платёж откроется на защищённой странице ЮKassa. После
                    подтверждения заказ появится в «Моих приложениях».
                  </p>
                </div>
              </li>
              <li>
                <span>2</span>
                <div>
                  <strong>Установите через Safari</strong>
                  <p>
                    Откройте персональную ссылку на iPhone и подтвердите
                    установку. Кабель и компьютер не нужны.
                  </p>
                </div>
              </li>
              <li>
                <span>3</span>
                <div>
                  <strong>Пройдите первый запуск</strong>
                  <p>
                    Если приложение не открывается, следуйте инструкции
                    активации. Может понадобиться временный вход в другой
                    аккаунт только в App Store.
                  </p>
                </div>
              </li>
            </ol>
          </div>
        </section>
        <aside className="checkout-card">
          <div className="eyebrow">ДОСТУП К УСТАНОВКЕ</div>
          <div className="checkout-price">{rubles(app.price_rub)}</div>
          {config?.payments_mode === "test" && (
            <>
              <TestBadge />
              <p className="small-text muted">
                Тестовый режим ЮKassa. Настоящие деньги не списываются.
              </p>
            </>
          )}
          <div className="checkout-includes">
            <div>
              <Icon name="download" size={18} />
              Ссылка на установку
            </div>
            <div>
              <Icon name="help" size={18} />
              Инструкция по активации
            </div>
            <div>
              <Icon name="stack" size={18} />
              Заказ в вашем профиле
            </div>
          </div>
          {!app.available ? (
            <Notice>Это приложение пока недоступно для установки.</Notice>
          ) : !user ? (
            <TelegramGate />
          ) : !config?.payments_configured ? (
            <Notice>
              Приём платежей ещё настраивается. Оплатить заказ можно будет после
              подключения магазина.
            </Notice>
          ) : (
            <Button className="full" busy={busy} onClick={checkout}>
              Получить доступ
              <Icon name="arrow" size={18} />
            </Button>
          )}
          {error && <Notice tone="error">{error}</Notice>}
          <p className="checkout-fine">
            Перед установкой сохраните настройки существующей копии. Доступ не
            переносит приложение на ваш Apple Account.{" "}
            <button onClick={() => navigate("/help")}>Условия и помощь</button>
          </p>
        </aside>
      </div>
    </>
  );
}
function Orders() {
  const { user, authReady, navigate } = useApp();
  const [orders, setOrders] = useState<Order[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const load = useCallback(async () => {
    if (!user) {
      setLoading(false);
      return;
    }
    setLoading(true);
    setError("");
    try {
      setOrders((await api<{ items: Order[] }>("/orders")).items);
    } catch (error) {
      setError(message(error));
    } finally {
      setLoading(false);
    }
  }, [user]);
  useEffect(() => {
    void load();
  }, [load]);
  return (
    <section className="narrow-page">
      <div className="page-heading">
        <span className="eyebrow muted">ВАШ ПРОФИЛЬ</span>
        <h1>Мои приложения</h1>
        <p>Заказы, установка и активация — всё здесь.</p>
      </div>
      {!authReady || (user && loading) ? (
        <Skeleton />
      ) : !user ? (
        <TelegramGate />
      ) : error ? (
        <>
          <Notice tone="error">{error}</Notice>
          <Button secondary onClick={load}>
            Попробовать ещё раз
          </Button>
        </>
      ) : orders.length === 0 ? (
        <div className="empty-state">
          <div className="empty-icon">
            <Icon name="stack" size={30} />
          </div>
          <h2>Здесь будут ваши приложения</h2>
          <p>
            Выберите первое в каталоге. После оформления заказа оно появится
            здесь.
          </p>
          <Button onClick={() => navigate("/")}>
            В каталог
            <Icon name="arrow" size={18} />
          </Button>
        </div>
      ) : (
        <div className="orders-list">
          {orders.map((order) => (
            <button
              className="order-row"
              key={order.id}
              onClick={() => navigate(routeForOrder(order.id))}
            >
              <AppIcon />
              <div className="order-row-info">
                <strong>{order.app_title}</strong>
                <span>
                  {formatDate(order.created_at)} · {rubles(order.amount_rub)}
                  {order.test ? " · тест" : ""}
                </span>
                <span className={`status ${order.status}`}>
                  {orderStatus(order.status)}
                </span>
              </div>
              <Icon name="arrow" size={21} />
            </button>
          ))}
        </div>
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
      toast("Не удалось скопировать. Выделите значение вручную.");
      setVisible(true);
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
            onClick={() => setVisible(!visible)}
            aria-label={visible ? "Скрыть пароль" : "Показать пароль"}
          >
            <Icon name="eye" size={18} />
          </button>
        )}
        <button
          onClick={copy}
          aria-label={`Скопировать ${label.toLowerCase()}`}
        >
          <Icon name="copy" size={18} />
        </button>
      </div>
    </label>
  );
}
function ActivationPanel({ order }: { order: Order }) {
  const { config } = useApp();
  const [data, setData] = useState<Activation | null>(null);
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(false);
  const [error, setError] = useState("");
  async function reveal() {
    if (open) {
      setOpen(false);
      setData(null);
      return;
    }
    setBusy(true);
    setError("");
    try {
      setData(
        await api<Activation>(
          `/orders/${encodeURIComponent(order.id)}/activation`,
        ),
      );
      setOpen(true);
    } catch (error) {
      setError(message(error));
    } finally {
      setBusy(false);
    }
  }
  const support = httpsURL(data?.support_url || config?.support_url);
  return (
    <div className="activation">
      <button
        className="activation-toggle"
        onClick={reveal}
        disabled={busy}
        aria-expanded={open}
      >
        <span>
          <Icon name="lock" size={19} />
          Первый запуск и активация
        </span>
        {busy ? (
          <span className="spinner" />
        ) : (
          <Icon className={open ? "rotated" : ""} name="down" size={19} />
        )}
      </button>
      {error && <Notice tone="error">{error}</Notice>}
      {open && data && (
        <div className="activation-content">
          <p className="preserve-lines">{data.instructions}</p>
          {data.mode === "credentials" && (
            <>
              {data.apple_id && (
                <SecretField label="Apple Account" value={data.apple_id} />
              )}{" "}
              {data.password && (
                <SecretField label="Пароль" value={data.password} password />
              )}
              <Notice>
                Входите только в App Store («Контент и покупки»). Основной
                аккаунт iCloud менять не нужно. Коды подтверждения вводите
                только в системном окне Apple.
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
              Связаться с поддержкой
              <Icon name="external" size={16} />
            </a>
          ) : data.mode === "manual" ? (
            <p className="muted small-text">
              Канал поддержки ещё настраивается. Данные активации появятся после
              настройки сервиса.
            </p>
          ) : null}
          <p className="small-text muted">
            После успешного запуска можно вернуться в свой аккаунт App Store.
            Повторная установка или обновление могут потребовать новую
            авторизацию.
          </p>
        </div>
      )}
    </div>
  );
}
function OrderPage({
  id,
  paymentReturn = false,
}: {
  id: string;
  paymentReturn?: boolean;
}) {
  const { user, authReady, navigate, toast } = useApp();
  const [order, setOrder] = useState<Order | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [checking, setChecking] = useState(false);
  const [grant, setGrant] = useState<Grant | null>(null);
  const [grantBusy, setGrantBusy] = useState(false);
  const [grantError, setGrantError] = useState("");
  const load = useCallback(
    async (quiet = false) => {
      if (!user || !id) {
        setLoading(false);
        return;
      }
      if (!quiet) setChecking(true);
      try {
        const item = await api<Order>(`/orders/${encodeURIComponent(id)}`);
        setOrder(item);
        setError("");
        if (item.status === "canceled" || item.status === "refunded")
          clearPurchaseRequest(item.app_slug);
      } catch (error) {
        setError(message(error));
      } finally {
        setLoading(false);
        setChecking(false);
      }
    },
    [id, user],
  );
  useEffect(() => {
    void load();
  }, [load]);
  useEffect(() => {
    if (order?.status !== "pending") return;
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") void load(true);
    }, 8000);
    const focus = () => {
      if (document.visibilityState === "visible") void load(true);
    };
    document.addEventListener("visibilitychange", focus);
    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", focus);
    };
  }, [order?.status, load]);
  async function retryPayment() {
    if (!order || checking) return;
    setChecking(true);
    setError("");
    try {
      setOrder(
        await api<Order>(
          `/orders/${encodeURIComponent(order.id)}/retry-payment`,
          { method: "POST" },
        ),
      );
    } catch (error) {
      setError(message(error));
    } finally {
      setChecking(false);
    }
  }
  async function installLink() {
    if (!order) return;
    setGrantBusy(true);
    setGrantError("");
    try {
      const data = await api<Grant>(
        `/orders/${encodeURIComponent(order.id)}/install-link`,
        { method: "POST" },
      );
      const url = httpsURL(data.url);
      if (!url || new URL(url).origin !== window.location.origin)
        throw new Error(
          "Сервер вернул некорректную ссылку установки. Обратитесь в поддержку.",
        );
      setGrant(data);
    } catch (error) {
      setGrantError(message(error));
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
      <section className="narrow-page">
        <Back label="Мои приложения" to="/orders" />
        <Skeleton />
      </section>
    );
  if (!user)
    return (
      <section className="narrow-page">
        <div className="page-heading">
          <h1>{paymentReturn ? "Проверим оплату в Telegram" : "Ваш заказ"}</h1>
          <p>
            {paymentReturn
              ? "Возврат со страницы оплаты ещё не подтверждает платёж. Откройте бота и раздел «Мои приложения», чтобы увидеть статус заказа."
              : "Откройте бота, чтобы увидеть свой заказ."}
          </p>
        </div>
        <TelegramGate />
      </section>
    );
  if (!order)
    return (
      <section className="narrow-page">
        <Back label="Мои приложения" to="/orders" />
        <Notice tone="error">{error || "Заказ не найден."}</Notice>
        <Button secondary busy={checking} onClick={() => load()}>
          Проверить ещё раз
        </Button>
      </section>
    );
  const paymentURL = httpsURL(order.payment_url);
  return (
    <section className="narrow-page">
      <Back label="Мои приложения" to="/orders" />
      <div className="order-heading">
        <AppIcon large />
        <div>
          <span className={`status ${order.status}`}>
            {orderStatus(order.status)}
          </span>
          <h1>{order.app_title}</h1>
          <p>
            {rubles(order.amount_rub)} · {formatDate(order.created_at)}
          </p>
        </div>
      </div>
      {order.test && (
        <div className="order-test">
          <TestBadge />
          <span>Реальные деньги не списываются.</span>
        </div>
      )}
      {error && <Notice tone="error">{error}</Notice>}
      {order.status === "pending" && (
        <div className="surface payment-panel">
          <div className="round-icon">
            <Icon name="lock" size={25} />
          </div>
          <h2>
            {paymentReturn
              ? "Проверяем результат оплаты"
              : "Осталось оплатить заказ"}
          </h2>
          <p>
            Оплата проходит на странице ЮKassa. Доступ появится, когда сервер
            подтвердит платёж. Можно вернуться сюда после оплаты.
          </p>
          {paymentURL ? (
            <Button className="full" onClick={() => external(paymentURL)}>
              Перейти к оплате
              <Icon name="external" size={18} />
            </Button>
          ) : (
            <>
              <Notice>
                Создание платежа не завершилось. Повторим запрос для этого же
                заказа: новый заказ не создаётся.
              </Notice>
              <Button className="full" busy={checking} onClick={retryPayment}>
                Повторить создание оплаты
              </Button>
            </>
          )}
          <Button
            secondary
            className="full"
            busy={checking}
            onClick={() => load()}
          >
            <Icon name="refresh" size={17} />
            Проверить оплату
          </Button>
          <p className="small-text muted">
            <span className="live-dot" /> Статус проверяется автоматически
          </p>
        </div>
      )}
      {order.status === "succeeded" && order.can_install && (
        <>
          <Notice tone="success">
            <strong>Доступ открыт</strong>
            <br />
            Теперь можно установить приложение на iPhone.
          </Notice>
          <div className="surface installation-panel">
            <div className="section-heading">
              <h2>Установка приложения</h2>
              <Icon name="download" size={22} />
            </div>
            <p>
              Получите персональную ссылку и откройте её в{" "}
              <strong>Safari на iPhone</strong>. Затем подтвердите системный
              запрос установки.
            </p>
            <p className="small-text muted">
              Если Happ уже установлен, сохраните его настройки. iPhone может
              попросить удалить текущую копию.
            </p>
            {!grant ? (
              <Button className="full" busy={grantBusy} onClick={installLink}>
                Получить ссылку установки
                <Icon name="arrow" size={18} />
              </Button>
            ) : (
              <div className="grant">
                <Button className="full" onClick={() => external(grant.url)}>
                  Открыть установку
                  <Icon name="external" size={18} />
                </Button>
                <Button secondary className="full" onClick={copyLink}>
                  <Icon name="copy" size={17} />
                  Скопировать для Safari
                </Button>
                <p className="small-text muted">
                  Ссылка действует до {formatTime(grant.expires_at)} по времени
                  вашего устройства. Не пересылайте её другим людям.
                </p>
                <button
                  className="text-link"
                  disabled={grantBusy}
                  onClick={installLink}
                >
                  {grantBusy ? "Обновляем…" : "Получить новую ссылку"}
                </button>
              </div>
            )}
            {grantError && <Notice tone="error">{grantError}</Notice>}
          </div>
          <ActivationPanel order={order} />
        </>
      )}
      {order.status === "succeeded" && !order.can_install && (
        <Notice>
          Платёж подтверждён, но установка пока недоступна. Обратитесь в
          поддержку в разделе «Помощь».
        </Notice>
      )}
      {(order.status === "canceled" || order.status === "refunded") && (
        <div className="surface payment-panel">
          <h2>
            {order.status === "canceled"
              ? "Оплата не завершена"
              : "Доступ закрыт после возврата"}
          </h2>
          <p>
            {order.status === "canceled"
              ? "Этот заказ не оплачен. Вы можете вернуться к приложению и создать новый заказ."
              : "По этому заказу оформлен возврат. Если есть вопросы, свяжитесь с поддержкой."}
          </p>
          <Button
            onClick={() =>
              navigate(
                order.status === "canceled"
                  ? `/apps/${order.app_slug}`
                  : "/help",
              )
            }
          >
            {order.status === "canceled" ? "К приложению" : "Открыть помощь"}
          </Button>
        </div>
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
    "Для этого способа установки достаточно iPhone и Safari. Вы получите временную ссылку. Системные запросы установки нужно подтвердить на самом телефоне.",
  ],
  [
    "Почему приложение не открывается?",
    "Одной загрузки может быть недостаточно: iOS проверяет права на приложение. В оплаченном заказе откройте «Первый запуск и активация». Если инструкция не помогла, обратитесь в поддержку и сообщите версию iOS и текст ошибки.",
  ],
  [
    "Нужно ли выходить из iCloud?",
    "Нет. Если требуется авторизация, она выполняется только в App Store или разделе «Контент и покупки». Основной аккаунт iCloud и «Локатор» менять не нужно. Никогда не вводите свой пароль Apple на странице этого сервиса.",
  ],
  [
    "Можно ли потом вернуться в свой аккаунт?",
    "В нашем тесте Happ продолжил работать после возврата в собственный аккаунт App Store. Это не гарантия для любой версии iOS и любого приложения. Обновление или повторная установка могут потребовать повторную авторизацию.",
  ],
  [
    "Что делать, если Happ уже установлен?",
    "Сначала сохраните ссылку подписки и настройки VPN. Система может потребовать удалить установленную копию; её локальные данные при этом могут потеряться. Проверьте запасной способ доступа в интернет до удаления.",
  ],
  [
    "Как устроена оплата и возврат?",
    "Платёж обрабатывает ЮKassa. Доступ выдаётся только после подтверждения сервером. В тестовом режиме реальные деньги не списываются. Если установка не удаётся или нужен возврат, обратитесь в поддержку с номером заказа.",
  ],
];
function Help() {
  const { config } = useApp();
  const support = httpsURL(config?.support_url);
  return (
    <section className="narrow-page">
      <div className="page-heading">
        <span className="eyebrow muted">ПО ШАГАМ, БЕЗ СЛОЖНОСТЕЙ</span>
        <h1>Поможем разобраться</h1>
        <p>Что происходит между кнопкой «Установить» и первым запуском.</p>
      </div>
      <div className="surface help-intro">
        <Icon name="phone" size={32} />
        <div>
          <h2>Сначала установка, затем запуск</h2>
          <p>
            Ссылка доставляет приложение на iPhone. Для работы iOS может
            отдельно запросить авторизацию аккаунта, через который получен файл.
          </p>
        </div>
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
        <div className="round-icon">
          <Icon name="help" size={27} />
        </div>
        <h2>Остался вопрос?</h2>
        <p>
          Напишите, на каком шаге нужна помощь. Для вопроса об оплате добавьте
          номер заказа.
        </p>
        {support ? (
          <a className="button" href={support} target="_blank" rel="noreferrer">
            Написать в поддержку
            <Icon name="arrow" size={18} />
          </a>
        ) : (
          <p className="small-text muted">
            Канал поддержки пока настраивается.
          </p>
        )}
      </div>
      <div className="terms">
        <h3>О тестовом сервисе</h3>
        <p>
          Аппки — экспериментальный сервис доставки приложений. Начальный
          каталог содержит Happ. Совместимость зависит от устройства, версии iOS
          и доступности авторизации Apple. Мы не связаны с Apple или
          разработчиком Happ.
        </p>
        <p>
          Не передавайте другим людям установочные ссылки и данные активации. Не
          отправляйте поддержке пароли и коды подтверждения вашего личного
          аккаунта. Информация об условиях коммерческой продажи и продавце будет
          опубликована до запуска реальных платежей.
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
  if (path === "/") return <Catalog />;
  if (path === "/orders") return <Orders />;
  if (path === "/help") return <Help />;
  if (path.startsWith("/apps/"))
    return <Detail key={path} slug={routePart(path.slice(6))} />;
  if (path.startsWith("/orders/"))
    return <OrderPage key={path} id={routePart(path.slice(8))} />;
  if (path === "/payment/return")
    return (
      <OrderPage
        id={new URLSearchParams(window.location.search).get("order_id") ?? ""}
        paymentReturn
      />
    );
  return (
    <section className="narrow-page">
      <Back />
      <div className="empty-state">
        <h1>Такой страницы нет</h1>
        <p>Все приложения доступны в каталоге.</p>
      </div>
    </section>
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
  const navigate = useCallback((route: string) => {
    window.history.pushState(null, "", route);
    setPath(new URL(route, window.location.origin).pathname);
    window.scrollTo({ top: 0, behavior: "instant" });
  }, []);
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
    } catch (error) {
      setError(message(error));
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => {
    void load();
    void authenticate()
      .then(setUser)
      .catch((error) => setAuthError(message(error)))
      .finally(() => setAuthReady(true));
    const tg = window.Telegram?.WebApp;
    tg?.ready();
    tg?.expand();
    tg?.setHeaderColor?.("#f6f5f1");
    tg?.setBackgroundColor?.("#f6f5f1");
    const pop = () =>
      setPath(window.location.pathname.replace(/\/$/, "") || "/");
    const unauthorized = () => {
      setUser(null);
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
    if (!announcement) return;
    const timer = window.setTimeout(() => setAnnouncement(""), 4500);
    return () => clearTimeout(timer);
  }, [announcement]);
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
      }}
    >
      <div className="app-shell">
        <Header />
        <main id="main">
          {loading ? (
            <Skeleton />
          ) : error ? (
            <div className="narrow-page">
              <Notice tone="error">{error}</Notice>
              <Button onClick={load}>Попробовать ещё раз</Button>
            </div>
          ) : (
            <Route />
          )}
        </main>
        <footer className="footer">
          <span>
            Аппки<span className="brand-dot">.</span>
          </span>
          <span>Нужные приложения рядом.</span>
          <button onClick={() => navigate("/help")}>Помощь и условия</button>
        </footer>
        <Navigation />
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
