import { useState } from "react";
import type { FormEvent, ReactNode } from "react";

import { AuthProvider, useAuth } from "./auth/AuthProvider";
import DraftCompanionScreen from "./DraftCompanionScreen";
import RaceScreen from "./RaceScreen";
import RhStorageStatusScreen from "./RhStorageStatusScreen";

type ModuleEntry = {
  name: string;
  description: string;
  href: string;
  status: string;
  publicModule?: boolean;
};

const modules: ModuleEntry[] = [
  {
    name: "Race Day",
    description: "Live Indy 500 pool standings for the family race day view.",
    href: "/race",
    status: "Public",
    publicModule: true,
  },
  {
    name: "Race Draft",
    description: "Draft companion for pool selection and available drivers.",
    href: "/race/draft",
    status: "Public",
    publicModule: true,
  },
  {
    name: "RH-Storage",
    description: "Storage pool health and repair status.",
    href: "/storage",
    status: "Authenticated",
  },
  {
    name: "Agent",
    description: "Deployment and agent workflow controls.",
    href: "/agent",
    status: "Planned",
  },
  {
    name: "Health",
    description: "Service health and operational status.",
    href: "/health",
    status: "Planned",
  },
];

function LoginScreen() {
  const { error, signIn } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setSubmitting(true);
    setFormError(null);

    try {
      await signIn(email, password);
    } catch (caught) {
      setFormError(
        caught instanceof Error ? caught.message : "Unable to sign in.",
      );
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <main className="flex min-h-screen items-center justify-center bg-slate-100 px-4 py-8 text-slate-950">
      <section className="w-full max-w-sm rounded-lg border border-slate-200 bg-white p-6 shadow-sm">
        <div className="mb-6">
          <div className="text-sm font-bold uppercase text-blue-600">
            RemiHub
          </div>
          <h1 className="mt-1 text-2xl font-black">Sign in</h1>
          <p className="mt-2 text-sm text-slate-600">
            Use your RemiHub Firebase account to access private portal modules.
          </p>
        </div>

        {(formError || error) && (
          <div className="mb-4 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm font-semibold text-red-700">
            {formError || error}
          </div>
        )}

        <form className="space-y-4" onSubmit={submit}>
          <label className="block">
            <span className="text-sm font-semibold text-slate-700">Email</span>
            <input
              autoComplete="email"
              className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-base outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-100"
              onChange={(event) => setEmail(event.target.value)}
              required
              type="email"
              value={email}
            />
          </label>

          <label className="block">
            <span className="text-sm font-semibold text-slate-700">
              Password
            </span>
            <input
              autoComplete="current-password"
              className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-base outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-100"
              onChange={(event) => setPassword(event.target.value)}
              required
              type="password"
              value={password}
            />
          </label>

          <button
            className="w-full rounded-md bg-slate-950 px-4 py-2.5 text-sm font-bold text-white hover:bg-slate-800 disabled:cursor-not-allowed disabled:bg-slate-400"
            disabled={submitting}
            type="submit"
          >
            {submitting ? "Signing in..." : "Sign in"}
          </button>
        </form>
      </section>
    </main>
  );
}

function AuthenticatedRoute({ children }: { children: ReactNode }) {
  const { status } = useAuth();

  if (status === "checking") {
    return (
      <main className="flex min-h-screen items-center justify-center bg-slate-100 text-sm font-semibold text-slate-600">
        Checking RemiHub session...
      </main>
    );
  }

  if (status === "unauthenticated") {
    return <LoginScreen />;
  }

  return <>{children}</>;
}

function PortalLayout({ children }: { children: ReactNode }) {
  const { signOut, user } = useAuth();

  return (
    <div className="min-h-screen bg-slate-100 text-slate-950">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-3 px-4 py-4">
          <a className="text-xl font-black" href="/">
            RemiHub
          </a>
          <nav aria-label="RemiHub modules" className="flex flex-wrap gap-2">
            {modules.map((module) => (
              <a
                className="rounded-md px-3 py-2 text-sm font-semibold text-slate-700 hover:bg-slate-100"
                href={module.href}
                key={module.name}
              >
                {module.name}
              </a>
            ))}
          </nav>
          <div className="flex items-center gap-3">
            <div className="max-w-[12rem] truncate text-sm text-slate-600">
              {user?.displayName || user?.email}
            </div>
            <button
              className="rounded-md border border-slate-300 bg-white px-3 py-2 text-sm font-bold text-slate-700 hover:bg-slate-50"
              onClick={signOut}
              type="button"
            >
              Sign out
            </button>
          </div>
        </div>
      </header>

      {children}
    </div>
  );
}

function PortalHome() {
  return (
    <PortalLayout>
      <main className="mx-auto max-w-7xl px-4 py-8">
        <div className="mb-6">
          <h1 className="text-3xl font-black">RemiHub Portal</h1>
          <p className="mt-2 max-w-2xl text-sm text-slate-600">
            Central access for RemiHub modules and operations.
          </p>
        </div>

        <section className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {modules.map((module) => (
            <a
              className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm transition hover:border-blue-300 hover:shadow"
              href={module.href}
              key={module.name}
            >
              <div className="flex items-start justify-between gap-3">
                <h2 className="text-lg font-black">{module.name}</h2>
                <span className="rounded-full bg-slate-100 px-2 py-1 text-xs font-bold text-slate-600">
                  {module.status}
                </span>
              </div>
              <p className="mt-3 text-sm leading-6 text-slate-600">
                {module.description}
              </p>
              {module.publicModule && (
                <div className="mt-4 text-xs font-bold uppercase text-blue-600">
                  Opens public experience
                </div>
              )}
            </a>
          ))}
        </section>
      </main>
    </PortalLayout>
  );
}

function PlaceholderModule({ title }: { title: string }) {
  return (
    <PortalLayout>
      <main className="mx-auto max-w-3xl px-4 py-8">
        <section className="rounded-lg border border-slate-200 bg-white p-6 shadow-sm">
          <div className="text-sm font-bold uppercase text-blue-600">
            RemiHub Portal
          </div>
          <h1 className="mt-1 text-2xl font-black">{title}</h1>
          <p className="mt-3 text-sm leading-6 text-slate-600">
            This portal module is not implemented in this card. The authenticated
            route is reserved so future work can add the full module without
            changing the shell.
          </p>
        </section>
      </main>
    </PortalLayout>
  );
}

function PrivateApp() {
  const path = window.location.pathname;

  if (path.startsWith("/storage")) {
    return (
      <PortalLayout>
        <RhStorageStatusScreen />
      </PortalLayout>
    );
  }

  if (path.startsWith("/agent")) {
    return <PlaceholderModule title="Agent" />;
  }

  if (path.startsWith("/health")) {
    return <PlaceholderModule title="Health" />;
  }

  return <PortalHome />;
}

function AppRoutes() {
  const path = window.location.pathname;

  if (path.startsWith("/race/draft")) {
    return <DraftCompanionScreen />;
  }

  if (path.startsWith("/race")) {
    return <RaceScreen />;
  }

  return (
    <AuthenticatedRoute>
      <PrivateApp />
    </AuthenticatedRoute>
  );
}

function App() {
  return (
    <AuthProvider>
      <AppRoutes />
    </AuthProvider>
  );
}

export default App;
