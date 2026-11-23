import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { logoutSession, requestOtp, verifyOtp } from "../api";

const AUTH_SESSION_KEY = "momentlens_otp_session";

type AuthSession = {
  access_token: string;
  token_type: string;
  expires_at: string;
  expires_in_seconds: number;
  email: string;
};

type OTPAuthContextValue = {
  session: AuthSession | null;
  email: string;
  isAuthenticated: boolean;
  requestCode: (email: string) => Promise<void>;
  verifyCode: (email: string, code: string) => Promise<void>;
  logout: () => void;
};

const OTPAuthContext = createContext<OTPAuthContextValue | null>(null);

function readSession(): AuthSession | null {
  try {
    const raw = localStorage.getItem(AUTH_SESSION_KEY) || sessionStorage.getItem(AUTH_SESSION_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as AuthSession;
    if (!parsed.access_token || !parsed.expires_at) return null;
    if (new Date(parsed.expires_at).getTime() <= Date.now()) return null;
    return parsed;
  } catch {
    return null;
  }
}

export function OTPAuthProvider({ children }: { children: React.ReactNode }) {
  const [session, setSession] = useState<AuthSession | null>(() => readSession());

  useEffect(() => {
    if (!session) return;
    const ms = new Date(session.expires_at).getTime() - Date.now();
    if (ms <= 0) {
      setSession(null);
      localStorage.removeItem(AUTH_SESSION_KEY);
      return;
    }
    const timer = window.setTimeout(() => {
      setSession(null);
      localStorage.removeItem(AUTH_SESSION_KEY);
    }, ms);
    return () => window.clearTimeout(timer);
  }, [session]);

  const requestCode = useCallback(async (email: string) => {
    await requestOtp(email.trim().toLowerCase());
  }, []);

  const verifyCode = useCallback(async (email: string, code: string) => {
    const next = await verifyOtp(email.trim().toLowerCase(), code.trim());
    localStorage.setItem(AUTH_SESSION_KEY, JSON.stringify(next));
    sessionStorage.setItem(AUTH_SESSION_KEY, JSON.stringify(next));
    setSession(next);
  }, []);

  const logout = useCallback(() => {
    logoutSession().catch(() => undefined);
    localStorage.removeItem(AUTH_SESSION_KEY);
    sessionStorage.removeItem(AUTH_SESSION_KEY);
    setSession(null);
  }, []);

  const value = useMemo<OTPAuthContextValue>(() => ({
    session,
    email: session?.email || "",
    isAuthenticated: !!session,
    requestCode,
    verifyCode,
    logout,
  }), [logout, requestCode, session, verifyCode]);

  return <OTPAuthContext.Provider value={value}>{children}</OTPAuthContext.Provider>;
}

export function useOTPAuth() {
  const value = useContext(OTPAuthContext);
  if (!value) throw new Error("useOTPAuth must be used inside OTPAuthProvider");
  return value;
}
