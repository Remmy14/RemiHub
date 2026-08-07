import { createContext, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";

import {
  signInWithEmailPassword,
  signOut,
  subscribeToAuth,
} from "./firebaseAuth";
import type { AuthSnapshot } from "./firebaseAuth";

type AuthContextValue = AuthSnapshot & {
  signIn: (email: string, password: string) => Promise<void>;
  signOut: () => void;
};

const AuthContext = createContext<AuthContextValue | null>(null);

const initialSnapshot: AuthSnapshot = {
  status: "checking",
  user: null,
  error: null,
};

export function AuthProvider({ children }: { children: ReactNode }) {
  const [snapshot, setSnapshot] = useState<AuthSnapshot>(initialSnapshot);

  useEffect(() => {
    return subscribeToAuth(setSnapshot);
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({
      ...snapshot,
      signIn: signInWithEmailPassword,
      signOut,
    }),
    [snapshot],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error("useAuth must be used inside AuthProvider");
  }

  return context;
}
