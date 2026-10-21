import { apiRequest } from "./authenticatedApi";

export type SnapshotItem = {
  source: string;
  source_account_id: string | null;
  label: string;
  institution_label: string | null;
  asset_category: string;
  value: number;
  confidence: string;
};

export type FinanceSummary = {
  snapshot_id: string;
  snapshot_month: string;
  status: string;
  total_assets: number;
  total_liabilities: number;
  net_worth: number;
  notes: string | null;
  items: SnapshotItem[];
  categories: { asset_category: string; value: number }[];
};

export type FinanceHistoryPoint = Pick<
  FinanceSummary,
  "snapshot_id" | "snapshot_month" | "total_assets" | "total_liabilities" | "net_worth" | "status"
>;

export type FinanceAccount = {
  id: string;
  source: string;
  source_account_id: string;
  institution_label: string | null;
  name: string;
  official_name: string | null;
  mask: string | null;
  type: string | null;
  subtype: string | null;
  asset_category: string;
  include_in_net_worth: boolean;
  display_name: string | null;
  display_order: number | null;
  updated_at: string | null;
};

export type FinanceAccountPatch = Partial<Pick<
  FinanceAccount,
  "include_in_net_worth" | "asset_category" | "display_name" | "display_order"
>>;

export type ManualAccount = {
  id: string;
  name: string;
  classification: "asset" | "liability";
  asset_category: string;
  current_balance: string;
  as_of_date: string;
  updated_at: string;
  include_in_net_worth: boolean;
  notes: string | null;
  active: boolean;
};

export type ManualAccountCreate = Pick<
  ManualAccount,
  "name" | "classification" | "asset_category" | "current_balance" |
  "as_of_date" | "include_in_net_worth" | "notes"
>;

export type ManualAccountPatch = Partial<ManualAccountCreate & Pick<ManualAccount, "active">>;

type Envelope<T> = { success: boolean; data: T };
const noStore: RequestInit = { cache: "no-store" };

function data<T>(response: Envelope<T>): T {
  if (!response.success) throw new Error("Finance request failed.");
  return response.data;
}

export const financeApi = {
  summary: () => apiRequest<FinanceSummary>("/finance/summary", noStore),
  history: (limit = 36) =>
    apiRequest<FinanceHistoryPoint[]>(`/finance/history?limit=${limit}`, noStore),
  accounts: async () => data(await apiRequest<Envelope<FinanceAccount[]>>(
    "/finance/accounts", noStore,
  )),
  updateAccount: async (id: string, patch: FinanceAccountPatch) =>
    data(await apiRequest<Envelope<FinanceAccount>>(
      `/finance/accounts/${encodeURIComponent(id)}`,
      { method: "PATCH", body: JSON.stringify(patch) },
    )),
  manualAccounts: async (includeArchived = true) =>
    data(await apiRequest<Envelope<ManualAccount[]>>(
      `/finance/manual-accounts?include_archived=${includeArchived}`, noStore,
    )),
  manualAccount: async (id: string) =>
    data(await apiRequest<Envelope<ManualAccount>>(
      `/finance/manual-accounts/${encodeURIComponent(id)}`, noStore,
    )),
  createManualAccount: async (request: ManualAccountCreate) =>
    data(await apiRequest<Envelope<ManualAccount>>(
      "/finance/manual-accounts", { method: "POST", body: JSON.stringify(request) },
    )),
  updateManualAccount: async (id: string, patch: ManualAccountPatch) =>
    data(await apiRequest<Envelope<ManualAccount>>(
      `/finance/manual-accounts/${encodeURIComponent(id)}`,
      { method: "PATCH", body: JSON.stringify(patch) },
    )),
  role: async () => data(await apiRequest<Envelope<{ role: string }>>("/auth/me", noStore)),
};
