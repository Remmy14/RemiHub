import { initializeApp } from "firebase/app";
import {
  getAuth,
  onAuthStateChanged,
  signInWithEmailAndPassword,
  signOut as firebaseSignOut,
} from "firebase/auth";
import type { User } from "firebase/auth";

import { firebaseBrowserConfig } from "../firebase-browser/firebaseBrowserConfig";

export type AuthUser = {
  email: string | null;
  displayName: string | null;
};

export type AuthSnapshot = {
  status: "checking" | "authenticated" | "unauthenticated";
  user: AuthUser | null;
  error: string | null;
};

type AuthListener = (snapshot: AuthSnapshot) => void;

const app = initializeApp(firebaseBrowserConfig);
const auth = getAuth(app);

let currentFirebaseUser: User | null = auth.currentUser;
let snapshot: AuthSnapshot = {
  status: "checking",
  user: null,
  error: null,
};
const listeners = new Set<AuthListener>();

function userFromFirebaseUser(user: User): AuthUser {
  return {
    email: user.email,
    displayName: user.displayName,
  };
}

function emit(nextSnapshot: AuthSnapshot) {
  snapshot = nextSnapshot;
  listeners.forEach((listener) => listener(snapshot));
}

onAuthStateChanged(
  auth,
  (user) => {
    currentFirebaseUser = user;
    if (!user) {
      emit({ status: "unauthenticated", user: null, error: null });
      return;
    }

    emit({
      status: "authenticated",
      user: userFromFirebaseUser(user),
      error: null,
    });
  },
  (error) => {
    currentFirebaseUser = null;
    emit({
      status: "unauthenticated",
      user: null,
      error: error.message,
    });
  },
);

export function subscribeToAuth(listener: AuthListener): () => void {
  listeners.add(listener);
  listener(snapshot);
  return () => listeners.delete(listener);
}

export async function signInWithEmailPassword(email: string, password: string) {
  await signInWithEmailAndPassword(auth, email, password);
}

export async function getFirebaseIdToken(): Promise<string> {
  const user = currentFirebaseUser ?? auth.currentUser;
  if (!user) {
    throw new Error("Authentication required.");
  }

  return user.getIdToken();
}

export async function signOut() {
  await firebaseSignOut(auth);
}

export function markAuthenticationExpired(message: string) {
  emit({ status: "unauthenticated", user: null, error: message });
}
