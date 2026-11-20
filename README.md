# Daily Close

A phone-first installable PWA for Ethiopian wholesalers and traders who sell on credit. It provides a Today dashboard, quick sale entry, overdue follow-up, a payment review queue, and a public shop-balance confirmation flow in English and Amharic.

## Run locally

```bash
npm install
npm run dev
```

Build with `npm run build` and serve the generated `dist/` directory over HTTPS. The app uses Vite and TypeScript and has no frontend runtime dependencies.

## Configuration

Copy `.env.example` to `.env.local` if you want to override the defaults:

- `VITE_MOCK_MODE=true` uses bundled synthetic fixtures and sends no backend requests. This is the default.
- `VITE_MOCK_MODE=false` enables the JSON API client.
- `VITE_API_BASE_URL=https://api.example.com` sets the browser-safe HTTPS API origin. Do not put secrets in frontend environment variables or source code.

All backend requests live in `src/api.ts`. The client expects a JSON adapter at these routes:

| Method | Route | Purpose |
| --- | --- | --- |
| GET | `/api/v1/dashboard/today` | Today totals and top overdue shops |
| GET | `/api/v1/shops/overdue` | Overdue list and shop choices |
| POST | `/api/v1/sales` | Submit an entered credit sale |
| GET | `/api/v1/review-queue` | Backend-suggested payment matches |
| POST | `/api/v1/review-queue/:id/approve` | Submit an approval decision |
| POST | `/api/v1/review-queue/:id/reject` | Submit a rejection decision |
| GET | `/api/v1/public/balances/:token` | Read a tokenized public shop balance |
| POST | `/api/v1/public/balances/:token/confirm` | Send buyer confirmation |
| POST | `/api/v1/public/balances/:token/dispute` | Send buyer dispute |

The uploaded FastAPI/PostgreSQL backend currently serves server-rendered HTML and form routes, not this JSON API. Keep mock mode on until a compatible adapter exists. The UI shows `Verified` only when the backend response says `verified`; every other status is displayed as `Unverified`. Receipt photos are never treated as proof of payment. The frontend does not match, void, re-match, or calculate payments.

## PWA and privacy

The service worker caches only the application shell and static app assets; it never caches API responses. Use HTTPS in production. No analytics, ad scripts, external font downloads, or frontend secrets are included.
