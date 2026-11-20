# Daily Close delivery outcomes

- [x] **Today dashboard.** Show total owed, overdue total, verified payments today and the top five overdue shops. Label only backend-reported payment verification as verified; provide loading, retry and error states.
- [x] **Credit-sale entry.** Provide a one-handed, mobile-friendly form with shop, ETB amount and due-in-days fields. Submit entered values through the centralized API client; do not calculate or reconcile payment amounts in the frontend.
- [x] **Overdue follow-up.** List overdue shops ordered by oldest overdue first, then largest balance, and provide one-tap phone-call and Telegram actions.
- [x] **Payment review queue.** Display backend-suggested payment matches with explicit Verified/Unverified status from the backend and Approve/Reject actions. A receipt photo on file is evidence only and must never be shown or treated as proof of payment.
- [x] **Public shop balance.** Provide a no-login tokenized balance page with buyer Confirm and Dispute actions, with results sent through the API client.
- [x] **English and Amharic.** Include a language toggle, render localized UI copy, set the document language, and use an Ethiopic-capable system font fallback.
- [x] **Safe API integration.** Centralize all backend calls in one client. Default to synthetic mock data and no backend requests; support an HTTPS API base URL when mock mode is disabled. Never put secrets or tracking scripts in the frontend. Treat the existing backend as HTML/form routes until a JSON adapter exists.
- [x] **Installable, resilient PWA.** Provide manifest, app icons and an app-shell-only service worker; do not cache API responses. Use high-contrast mobile-first layout, 48px minimum touch targets, ETB display formatting, and a small bundle suitable for slow mobile data.
