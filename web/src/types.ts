export interface AppInfo {
  slug: string;
  title: string;
  subtitle: string;
  description: string;
  version: string;
  size_bytes: number;
  minimum_ios: string;
  price_rub: string;
  category: string;
  features: string[];
  available: boolean;
}
export interface Config {
  brand: string;
  bot_username: string;
  support_url: string;
  payments_mode: string;
  payments_configured: boolean;
  telegram_configured: boolean;
}
export interface User {
  id: number;
  first_name: string;
  username?: string;
}
export interface Order {
  id: string;
  app_slug: string;
  app_title: string;
  amount_rub: string;
  currency: "RUB";
  status: "pending" | "succeeded" | "canceled" | "refunded";
  payment_url?: string;
  created_at: string;
  paid_at?: string;
  test: boolean;
  can_install: boolean;
}
export interface Activation {
  mode: "manual" | "credentials";
  apple_id?: string;
  password?: string;
  instructions: string;
  support_url: string;
}
export interface Grant {
  url: string;
  expires_at: string;
}
export interface TelegramWebApp {
  initData: string;
  ready: () => void;
  expand: () => void;
  openLink: (url: string, options?: { try_instant_view?: boolean }) => void;
  openTelegramLink?: (url: string) => void;
  setHeaderColor?: (color: string) => void;
  setBackgroundColor?: (color: string) => void;
}
declare global {
  interface Window {
    Telegram?: { WebApp?: TelegramWebApp };
  }
}
