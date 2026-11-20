export type Language = 'en' | 'am';
export type Verification = 'verified' | 'unverified';
export type Shop = { id: string; name: string; phone: string; telegram: string; owed: number; overdueDays: number; lastSale: string };
export type Dashboard = { totalOwed: number; overdueTotal: number; paidToday: number; topOverdue: Shop[] };
export type ReviewItem = { id: string; shopName: string; amount: number; submittedAt: string; verification: Verification; receiptPhotoOnFile: boolean; source: string };
export type Balance = { shopId: string; shopName: string; owed: number; overdue: number; updatedAt: string };
export type SaleInput = { shopId: string; amount: number; dueInDays: number };

// Auth types
export type AuthUser = { id: string; displayName: string; role: string; tenantId: string; tenantName: string };
export type LoginResponse = { ok: boolean; token: string; user: AuthUser };

const TOKEN_KEY = 'dailyclose-token';
const USER_KEY = 'dailyclose-user';

const config = {
  mock: import.meta.env.VITE_MOCK_MODE !== 'false',
  baseUrl: String(import.meta.env.VITE_API_BASE_URL || '').replace(/\/$/, ''),
};

// These versioned JSON routes need an adapter in the uploaded FastAPI backend.
// Its current routes return HTML/forms, so mock mode is the safe default.
const routes = {
  dashboard: '/api/v1/dashboard/today',
  overdue: '/api/v1/shops/overdue',
  sales: '/api/v1/sales',
  reviewQueue: '/api/v1/review-queue',
  publicBalances: '/api/v1/public/balances',
  authLogin: '/api/v1/auth/login',
  authLogout: '/api/v1/auth/logout',
  authMe: '/api/v1/auth/me',
} as const;

const shops: Shop[] = [
  { id: 'shop-1', name: 'Kebede Mini Market', phone: '+251 911 234 567', telegram: 'kebede_minimarket', owed: 126400, overdueDays: 18, lastSale: '2 days ago' },
  { id: 'shop-2', name: 'Selam Wholesale', phone: '+251 922 555 010', telegram: 'selam_wholesale', owed: 89200, overdueDays: 12, lastSale: 'yesterday' },
  { id: 'shop-3', name: 'Bole Corner Shop', phone: '+251 933 120 084', telegram: 'bole_corner', owed: 64500, overdueDays: 9, lastSale: '4 days ago' },
  { id: 'shop-4', name: 'Meklit Grocers', phone: '+251 911 908 211', telegram: 'meklit_grocers', owed: 48200, overdueDays: 7, lastSale: 'today' },
  { id: 'shop-5', name: 'Addis Fresh Mart', phone: '+251 944 003 390', telegram: 'addis_fresh', owed: 31600, overdueDays: 4, lastSale: '3 days ago' },
  { id: 'shop-6', name: 'Kirkos Traders', phone: '+251 955 017 220', telegram: 'kirkos_traders', owed: 18800, overdueDays: 2, lastSale: 'today' },
];

const mockDashboard: Dashboard = { totalOwed: 378700, overdueTotal: 359900, paidToday: 42800, topOverdue: shops.slice(0, 5) };
const mockReviews: ReviewItem[] = [
  { id: 'review-1', shopName: 'Selam Wholesale', amount: 32000, submittedAt: 'Today, 09:18', verification: 'verified', receiptPhotoOnFile: true, source: 'Telebirr statement line' },
  { id: 'review-2', shopName: 'Meklit Grocers', amount: 18500, submittedAt: 'Yesterday, 17:42', verification: 'unverified', receiptPhotoOnFile: true, source: 'Receipt photo only' },
  { id: 'review-3', shopName: 'Kebede Mini Market', amount: 54000, submittedAt: 'Yesterday, 13:07', verification: 'verified', receiptPhotoOnFile: false, source: 'Buyer confirmation' },
];
// Fixed display fixtures. No balance or payment status is derived in the client.
const mockBalances: Record<string, Balance> = {
  'shop-1': { shopId: 'shop-1', shopName: 'Kebede Mini Market', owed: 126400, overdue: 78368, updatedAt: 'Today, 08:30' },
  'shop-2': { shopId: 'shop-2', shopName: 'Selam Wholesale', owed: 89200, overdue: 55304, updatedAt: 'Today, 08:30' },
  'shop-3': { shopId: 'shop-3', shopName: 'Bole Corner Shop', owed: 64500, overdue: 39990, updatedAt: 'Today, 08:30' },
  'shop-4': { shopId: 'shop-4', shopName: 'Meklit Grocers', owed: 48200, overdue: 29884, updatedAt: 'Today, 08:30' },
  'shop-5': { shopId: 'shop-5', shopName: 'Addis Fresh Mart', owed: 31600, overdue: 19592, updatedAt: 'Today, 08:30' },
  'shop-6': { shopId: 'shop-6', shopName: 'Kirkos Traders', owed: 18800, overdue: 11656, updatedAt: 'Today, 08:30' },
};

async function mockResponse<T>(path: string, init?: RequestInit): Promise<T> {
  await new Promise((resolve) => window.setTimeout(resolve, 140));
  if (path === routes.dashboard) return { ...mockDashboard, topOverdue: mockDashboard.topOverdue.map((shop) => ({ ...shop })) } as T;
  if (path === routes.overdue) return shops.map((shop) => ({ ...shop })) as T;
  if (path === routes.reviewQueue) return mockReviews.map((item) => ({ ...item })) as T;

  const balanceMatch = path.match(/^\/api\/v1\/public\/balances\/([^/]+)(?:\/(confirm|dispute))?$/);
  if (balanceMatch) {
    const token = decodeURIComponent(balanceMatch[1]);
    if (balanceMatch[2]) return { ok: true } as T;
    const balance = mockBalances[token];
    if (!balance) throw new Error('Balance link not found.');
    return { ...balance } as T;
  }
  if (path === routes.sales && init?.method === 'POST') return { ok: true } as T;
  if (path.startsWith(`${routes.reviewQueue}/`) && init?.method === 'POST') return { ok: true } as T;
  throw new Error('This mock route is not available.');
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  if (config.mock) return mockResponse<T>(path, init);
  if (!config.baseUrl.startsWith('https://')) throw new Error('Set VITE_API_BASE_URL to an HTTPS API origin.');

  const token = localStorage.getItem(TOKEN_KEY);
  const authHeader: Record<string, string> = token ? { Authorization: `Bearer ${token}` } : {};

  let response: Response;
  try {
    response = await fetch(`${config.baseUrl}${path}`, {
      ...init,
      headers: { Accept: 'application/json', 'Content-Type': 'application/json', ...authHeader, ...init?.headers },
    });
  } catch {
    throw new Error('Could not reach the API. Check your connection and try again.');
  }

  if (response.status === 401) {
    // Session expired or invalid – clear stored credentials and signal the app.
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(USER_KEY);
    window.dispatchEvent(new CustomEvent('dc:unauthorized'));
    throw new Error('Your session has expired. Please sign in again.');
  }

  if (!response.ok) throw new Error(`The API returned ${response.status}. Try again or contact your administrator.`);
  try {
    return await response.json() as T;
  } catch {
    throw new Error('The API did not return JSON. The JSON adapter may not be available yet.');
  }
}

export const api = {
  mode: () => config.mock ? 'MOCK' : 'REAL',

  // ---------------------------------------------------------------------------
  // Auth
  // ---------------------------------------------------------------------------
  isAuthenticated(): boolean {
    return Boolean(localStorage.getItem(TOKEN_KEY));
  },

  getStoredUser(): AuthUser | null {
    try {
      const raw = localStorage.getItem(USER_KEY);
      return raw ? (JSON.parse(raw) as AuthUser) : null;
    } catch {
      return null;
    }
  },

  async login(identifier: string, password: string): Promise<LoginResponse> {
    if (config.mock) {
      await new Promise((r) => window.setTimeout(r, 400));
      const mockUser: AuthUser = { id: 'mock-user', displayName: 'Demo Owner', role: 'owner', tenantId: 'mock-tenant', tenantName: 'Demo Company' };
      const mockToken = 'mock-token-' + Date.now();
      localStorage.setItem(TOKEN_KEY, mockToken);
      localStorage.setItem(USER_KEY, JSON.stringify(mockUser));
      return { ok: true, token: mockToken, user: mockUser };
    }
    // Real mode: use baseUrl but allow http://localhost for dev
    const url = `${config.baseUrl}${routes.authLogin}`;
    let response: Response;
    try {
      response = await fetch(url, {
        method: 'POST',
        headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
        body: JSON.stringify({ identifier, password }),
      });
    } catch {
      throw new Error('Could not reach the API. Check your connection and try again.');
    }
    if (!response.ok) {
      let detail = `Login failed (${response.status}).`;
      try { const j = await response.json() as { detail?: string }; detail = j.detail ?? detail; } catch { /* ignore */ }
      throw new Error(detail);
    }
    const data = await response.json() as LoginResponse;
    localStorage.setItem(TOKEN_KEY, data.token);
    localStorage.setItem(USER_KEY, JSON.stringify(data.user));
    return data;
  },

  async logout(): Promise<void> {
    try {
      await request<{ ok: boolean }>(routes.authLogout, { method: 'POST' });
    } catch { /* best-effort */ }
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(USER_KEY);
  },

  getMe: () => request<LoginResponse>(routes.authMe),

  // ---------------------------------------------------------------------------
  // Data endpoints
  // ---------------------------------------------------------------------------
  getDashboard: () => request<Dashboard>(routes.dashboard),
  getOverdueShops: () => request<Shop[]>(routes.overdue),
  createSale: (input: SaleInput) => request<{ ok: boolean }>(routes.sales, { method: 'POST', body: JSON.stringify(input) }),
  getReviewQueue: () => request<ReviewItem[]>(routes.reviewQueue),
  approveReview: (id: string) => request<{ ok: boolean }>(`${routes.reviewQueue}/${encodeURIComponent(id)}/approve`, { method: 'POST' }),
  rejectReview: (id: string) => request<{ ok: boolean }>(`${routes.reviewQueue}/${encodeURIComponent(id)}/reject`, { method: 'POST' }),
  getShopBalance: (token: string) => request<Balance>(`${routes.publicBalances}/${encodeURIComponent(token)}`),
  confirmShopBalance: (token: string) => request<{ ok: boolean }>(`${routes.publicBalances}/${encodeURIComponent(token)}/confirm`, { method: 'POST' }),
  disputeShopBalance: (token: string) => request<{ ok: boolean }>(`${routes.publicBalances}/${encodeURIComponent(token)}/dispute`, { method: 'POST' }),
};
