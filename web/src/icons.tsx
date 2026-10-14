import type { CSSProperties, ReactNode } from "react";
const chatgptLogo = new URL("./assets/app-icons/chatgpt.svg", import.meta.url)
  .href;
const deepseekLogo = new URL("./assets/app-icons/deepseek.svg", import.meta.url)
  .href;
export function Icon({
  name,
  size = 20,
  className = "",
}: {
  name: string;
  size?: number;
  className?: string;
}) {
  const paths: Record<string, ReactNode> = {
    grid: (
      <>
        <rect x="3" y="3" width="7" height="7" rx="2" />
        <rect x="14" y="3" width="7" height="7" rx="2" />
        <rect x="3" y="14" width="7" height="7" rx="2" />
        <rect x="14" y="14" width="7" height="7" rx="2" />
      </>
    ),
    stack: (
      <>
        <path d="m12 3 9 5-9 5-9-5 9-5Z" />
        <path d="m3 12 9 5 9-5M3 16l9 5 9-5" />
      </>
    ),
    guide: (
      <>
        <rect x="4" y="3" width="16" height="18" rx="2" />
        <path d="M8 7h8M8 11h8M8 15h5" />
      </>
    ),
    help: (
      <>
        <circle cx="12" cy="12" r="9" />
        <path d="M9.3 8.7a2.8 2.8 0 0 1 5.4 1c0 2-2.7 2.3-2.7 4M12 17h.01" />
      </>
    ),
    arrow: (
      <>
        <path d="M5 12h14m-6-6 6 6-6 6" />
      </>
    ),
    back: <path d="m14 6-6 6 6 6" />,
    chevronRight: <path d="m9 6 6 6-6 6" />,
    down: <path d="m6 9 6 6 6-6" />,
    basket: (
      <>
        <path d="m7 10 5-7 5 7M3 10h18l-2 10H5L3 10ZM9 14v3m6-3v3" />
      </>
    ),
    bank: (
      <>
        <path d="m3 9 9-6 9 6H3ZM4 21h16M6 10v7m6-7v7m6-7v7M4 18h16" />
      </>
    ),
    check: <path d="m5 12 4 4L19 6" />,
    lock: (
      <>
        <rect x="5" y="10" width="14" height="11" rx="3" />
        <path d="M8 10V7a4 4 0 0 1 8 0v3M12 15v2" />
      </>
    ),
    phone: (
      <>
        <rect x="6" y="2" width="12" height="20" rx="3" />
        <path d="M10 5h4M11 19h2" />
      </>
    ),
    download: (
      <>
        <path d="M12 3v12m-5-5 5 5 5-5M5 16v4h14v-4" />
      </>
    ),
    external: (
      <>
        <path d="M14 3h7v7m0-7-10 10M10 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-5" />
      </>
    ),
    copy: (
      <>
        <rect x="8" y="8" width="12" height="13" rx="2" />
        <path d="M16 8V3H3v13h5" />
      </>
    ),
    close: <path d="m6 6 12 12M6 18 18 6" />,
    sparkle: (
      <>
        <path d="m12 3 2.4 6.6L21 12l-6.6 2.4L12 21l-2.4-6.6L3 12l6.6-2.4L12 3Z" />
      </>
    ),
    telegram: (
      <>
        <path d="m21 3-4 18-6-5-4 4 1-7 13-10ZM8 13l9-6-6 9M8 13l-6-3L21 3" />
      </>
    ),
    shield: (
      <>
        <path d="m12 2 8 3v6c0 5-8 11-8 11S4 16 4 11V5l8-3Z" />
        <path d="m8 11 3 3 5-5" />
      </>
    ),
    refresh: (
      <>
        <path d="M20 7v5h-5M4 17v-5h5" />
        <path d="M6 7a7 7 0 0 1 12-2l2 3M4 16l2 3a7 7 0 0 0 12-2" />
      </>
    ),
    search: (
      <>
        <circle cx="10.5" cy="10.5" r="7" />
        <path d="m16 16 5 5" />
      </>
    ),
    eye: (
      <>
        <path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7S2 12 2 12Z" />
        <circle cx="12" cy="12" r="3" />
      </>
    ),
  };
  return (
    <svg
      className={className}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {paths[name] ?? paths.grid}
    </svg>
  );
}
export function AppIcon({
  slug = "happ",
  title,
  large = false,
}: {
  slug?: string;
  title?: string;
  large?: boolean;
}) {
  const normalized = slug.toLowerCase().replace(/[-_]/g, "");
  const variants: Record<string, string> = {
    happ: "happ",
    v2ray: "v2ray",
    v2raytun: "v2ray",
    sber: "sber",
    sberbank: "sber",
    tbank: "tbank",
    tinkoff: "tbank",
    vtb: "vtb",
    vtbbank: "vtb",
    chatgpt: "chatgpt",
    deepseek: "deepseek",
  };
  const variant = variants[normalized] ?? "generic";
  const marks: Record<string, ReactNode> = {
    happ: (
      <svg viewBox="0 0 64 64" fill="none">
        <path
          d="M19 14v36M45 14v36M19 32h26"
          stroke="currentColor"
          strokeWidth="9"
          strokeLinecap="round"
        />
      </svg>
    ),
    v2ray: <span>V2</span>,
    sber: (
      <svg viewBox="0 0 64 64" fill="none">
        <path
          d="M52 27a22 22 0 1 1-10-15"
          stroke="currentColor"
          strokeWidth="5"
          strokeLinecap="round"
        />
        <path
          d="m20 29 10 10 25-24"
          stroke="currentColor"
          strokeWidth="5"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    ),
    tbank: <span>Т</span>,
    vtb: (
      <svg viewBox="0 0 64 64" fill="currentColor">
        <path d="M14 15h41l-5 9H9l5-9ZM10 29h34l-5 9H5l5-9ZM6 43h27l-5 9H1l5-9Z" />
      </svg>
    ),
    chatgpt: <img src={chatgptLogo} alt="" />,
    deepseek: <img src={deepseekLogo} alt="" />,
    generic: <span>{(title || slug).slice(0, 2).toUpperCase()}</span>,
  };
  return (
    <div
      className={`app-icon app-icon--${variant}${large ? " large" : ""}`}
      aria-hidden="true"
    >
      {marks[variant]}
    </div>
  );
}
export function BrandMark({ style }: { style?: CSSProperties }) {
  return (
    <span className="brand-mark" style={style}>
      <Icon name="grid" size={22} />
    </span>
  );
}
