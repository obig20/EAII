import './styles.css';
import { api, type AuthUser, type Balance, type Dashboard, type Language, type ReviewItem, type Shop } from './api';
import './register-sw';

type Copy = Record<string, string>;
const copy: Record<Language, Copy> = {
  en: {
    today: 'Today', todaySub: 'Your credit ledger, at a glance.', addSale: 'Add sale', overdue: 'Overdue', review: 'Review',
    totalOwed: 'Total owed', overdueTotal: 'Overdue total', paidToday: 'Verified today', topOverdue: 'Top overdue shops', daysLate: 'days late',
    saleTitle: 'Record a credit sale', chooseShop: 'Choose a shop', shop: 'Shop', amount: 'Amount (ETB)', due: 'Due in', days: 'days', saveSale: 'Save sale', saved: 'Sale sent',
    saleNote: 'Your entry is sent to the backend. Payment matching and money calculations are not done here.', queue: 'Suggested payment matches',
    verified: 'Verified', unverified: 'Unverified', receipt: 'Receipt photo on file – not counted as paid', approve: 'Approve', reject: 'Reject',
    balance: 'Shop balance', confirm: 'Confirm balance', dispute: 'Dispute balance', public: 'Public balance link · no login', updated: 'Updated',
    retry: 'Try again', loading: 'Loading…', call: 'Call shop', telegram: 'Telegram', noShops: 'No overdue shops to show.', noMatches: 'No suggested matches right now.',
    overviewTip: 'Only payment status reported as verified by the backend appears as Verified.',
    overdueIntro: 'Oldest overdue first, then largest balance. Reach out in one tap.',
    reviewIntro: 'Review backend suggestions. A receipt photo is evidence on file, not a paid status.',
    confirmNote: 'Confirm only if this balance matches your records. Dispute it if something looks wrong.',
    actionSent: 'Request sent', disputeSent: 'Dispute sent', errorTitle: 'This view could not load', language: 'Switch language', brand: 'Daily Close',
    overdueKicker: 'FOLLOW-UP LIST', reviewKicker: 'PAYMENT REVIEW', saleKicker: 'QUICK ENTRY', balanceKicker: 'SHARED STATEMENT', backToday: 'Back to today',
    // Auth
    loginTitle: 'Sign in', identifier: 'Email / Phone / Username', password: 'Password', signIn: 'Sign in', signOut: 'Sign out',
    loginError: 'Invalid credentials. Please try again.',
  },
  am: {
    today: 'ዛሬ', todaySub: 'የብድር መዝገብዎን በአንድ እይታ ይመልከቱ።', addSale: 'ሽያጭ ጨምር', overdue: 'ያለሙከሪ', review: 'ግምገማ',
    totalOwed: 'ጠቅላላ ዕዳ', overdueTotal: 'ያለሙ ዕዳ', paidToday: 'ዛሬ የተረጋገጠ', topOverdue: 'ቀደሚ የዘገዩ ሱቆች', daysLate: 'ቀን ዘግይቷ',
    saleTitle: 'የብድር ሽያጭ መዝግብ', chooseShop: 'ሱቅ ይምረጡ', shop: 'ሱቅ', amount: 'መጠን (ETB)', due: 'የሚከፈሉበት ቀን', days: 'ቀን', saveSale: 'ሽያጩን መዝግብ', saved: 'ሽያጭ ተመዝግቧ',
    saleNote: 'የጀመሩ መዘዝ ወደ ባክኤንድ ይሄዳሉ። የክፍያ ጣጣሚ ወይም የሒሳብ ስሌቶ እዚህ አይደረጋሉ።', queue: 'የተጠቆሙ የክፍያ ጣጣሚዎች',
    verified: 'ተረጋግቷ', unverified: 'አምተረጋገጠህ', receipt: 'የደረሰኝ ፎቶ ተቀምጧ – እንደተከፈለ አይቆጠርም', approve: 'አጽድቅ', reject: 'አትቀቡ',
    balance: 'የሱቅ ዕዳ', confirm: 'ዕዳውን አረጋግጥ', dispute: 'አከራከር', public: 'የህዝቡ የዕዳ አገናኝ · መግቢያ አይስፈልግም', updated: 'የተዘምነ',
    retry: 'እንደገና ሞክር', loading: 'በመጫን ላይ…', call: 'ሱቁን ደውለ', telegram: 'ቴሌግራም', noShops: 'ለማሳየት ያለሙከሪ የለም።', noMatches: 'አሁን የተጠቆሜ ጣጣሚ የለም።',
    overviewTip: 'ባክኤንዱ እንደተረጋገጠ ሲዘግብ ብቻ እንደ Verified ይታያሉ።',
    overdueIntro: 'በዘገዩበት ቀን ቅደሚ ይደረደራሉ፤ ቀዠሉ ሒሳብ ቀዠ። በአንድ ንካ ያሳጋጡ።',
    reviewIntro: 'የባክኤንዱ ጥቆሞዎን ይመልከቱ። የደረሰኝ ፎቶ ማስረጃ ነው፤ ክፍያ መርጋቱን አይረጋጥም።',
    confirmNote: 'ይህ ዕዳ ከመዝገብዎ ጋር ቢስማማ ብቻ ያረጋግጡ። ልዩነት ካለ ያከራክሩ።',
    actionSent: 'ጥያቄ ተመዝግቷ', disputeSent: 'ክርክር ተመዝግቷ', errorTitle: 'ይህ ገጽ መጫን አልቻለም', language: 'ቋንቋ ቀይር', brand: 'የዕለት መዝጊያ',
    overdueKicker: 'የክትትል ዝርዝር', reviewKicker: 'የክፍያ ግምገማ', saleKicker: 'ፍጣን መዝገብ', balanceKicker: 'የተጋራ መጠለፊ', backToday: 'ወደ ዛሬ ተመለስ',
    // Auth
    loginTitle: 'ይግቡ', identifier: 'ኢሜይል / ስልክ / ተጠቃሚ', password: 'የምስጢር ቁጥር', signIn: 'ይግቡ', signOut: 'ይውጡ',
    loginError: 'መረጃዎ ትክክለኛ አይደለም። እባክዎ እንደገና ይሞክሩ።',
  },
};

const app = document.querySelector<HTMLDivElement>('#app');
if (!app) throw new Error('Daily Close could not find its app root.');
function readLanguage(): Language {
  try { return localStorage.getItem('dailyclose-lang') === 'am' ? 'am' : 'en'; } catch { return 'en'; }
}
const currentRoute = () => location.hash ? location.hash.slice(1) : location.pathname !== '/' ? location.pathname : '/';
const state: { lang: Language; route: string; dashboard: Dashboard | null; shops: Shop[]; reviews: ReviewItem[]; balance: Balance | null; user: AuthUser | null } = {
  lang: readLanguage(), route: currentRoute(), dashboard: null, shops: [], reviews: [], balance: null, user: api.getStoredUser(),
};
const t = () => copy[state.lang];
const money = (value: number) => `${new Intl.NumberFormat('en-US', { maximumFractionDigits: 2 }).format(value)} ETB`;
const esc = (value: unknown) => String(value ?? '').replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' })[char]!);
const message = (error: unknown) => error instanceof Error ? error.message : 'Please try again.';

function icon(name: 'home' | 'plus' | 'clock' | 'check' | 'phone' | 'send' | 'arrow' | 'shield' | 'logout') {
  const paths: Record<typeof name, string> = {
    home: '<path d="m3 10 9-7 9 7v10a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z"/>', plus: '<path d="M12 5v14M5 12h14"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>', check: '<path d="m5 12 4 4L19 6"/>',
    phone: '<path d="M5 4h4l2 5-2.5 1.5a14 14 0 0 0 5 5L15 13l5 2v4a1 1 0 0 1-1 1C10 20 4 14 4 5a1 1 0 0 1 1-1Z"/>',
    send: '<path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13"/>', arrow: '<path d="M5 12h14m-6-6 6 6-6 6"/>',
    shield: '<path d="M12 22s8-4 8-11V5l-8-3-8 3v6c0 7 8 11 8 11Z"/><path d="m9 12 2 2 4-4"/>',
    logout: '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/>',
  };
  return `<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name]}</svg>`;
}

function navItem(route: string, iconName: 'home' | 'plus' | 'clock' | 'check', label: string) {
  const active = state.route === route;
  return `<button class="nav-item${active ? ' active' : ''}" type="button" data-route="${route}" aria-current="${active ? 'page' : 'false'}">${icon(iconName)}<span>${esc(label)}</span></button>`;
}
function shell(content: string, title: string, subtitle = '') {
  document.documentElement.lang = state.lang;
  document.documentElement.dir = 'ltr';
  const signOutBtn = api.isAuthenticated()
    ? `<button class="lang-toggle" id="sign-out-btn" type="button" aria-label="${esc(t().signOut)}" title="${esc(t().signOut)}">${icon('logout')}</button>`
    : '';
  app!.innerHTML = `<div class="app-shell">
    <header class="topbar">
      <a class="brand" href="#/" data-route="/" aria-label="${esc(t().brand)} – ${esc(t().backToday)}"><img src="/icons/icon.svg" alt="" width="32" height="32"><span>${esc(t().brand)}</span><span class="mode-pill"><i></i>${api.mode()}</span></a>
      <button class="lang-toggle" id="lang-toggle" type="button" aria-label="${esc(t().language)}">${state.lang === 'en' ? 'አማ' : 'EN'}</button>
      ${signOutBtn}
      <div class="page-heading"><h1>${esc(title)}</h1>${subtitle ? `<p>${esc(subtitle)}</p>` : ''}</div>
    </header>
    <main id="main-content">${content}</main>
    <nav class="bottom-nav" aria-label="${state.lang === 'am' ? 'ዋና ምናሌ' : 'Main navigation'}">${navItem('/', 'home', t().today)}${navItem('/add-sale', 'plus', t().addSale)}${navItem('/overdue', 'clock', t().overdue)}${navItem('/review', 'check', t().review)}</nav>
  </div>`;
  document.querySelector<HTMLButtonElement>('#lang-toggle')?.addEventListener('click', () => {
    state.lang = state.lang === 'en' ? 'am' : 'en';
    try { localStorage.setItem('dailyclose-lang', state.lang); } catch { /* keep the choice for this view */ }
    render();
  });
  document.querySelector<HTMLButtonElement>('#sign-out-btn')?.addEventListener('click', async () => {
    await api.logout();
    state.user = null;
    render();
  });
  document.querySelectorAll<HTMLElement>('[data-route]').forEach((element) => element.addEventListener('click', (event) => {
    event.preventDefault(); navigate(element.dataset.route || '/');
  }));
}
function navigate(route: string) { if (route !== state.route || location.hash !== `#${route}`) location.hash = route; }
function loading(title: string, subtitle = '') {
  shell(`<section class="loading-panel" role="status"><span class="spinner"></span><p>${esc(t().loading)}</p></section>`, title, subtitle);
}
function showError(messageText: string, retry: () => void, title = t().today) {
  shell(`<section class="empty-state error-state"><span class="state-icon">!</span><h2>${esc(t().errorTitle)}</h2><p>${esc(messageText)}</p><button class="button primary" id="retry" type="button">${esc(t().retry)}</button></section>`, title);
  document.querySelector<HTMLButtonElement>('#retry')?.addEventListener('click', retry);
}
function metric(label: string, value: number, tone: 'feature' | 'amber' | 'blue') {
  const symbol = tone === 'feature' ? icon('arrow') : tone === 'amber' ? icon('clock') : icon('shield');
  return `<article class="metric-card ${tone}"><div class="metric-label"><span>${esc(label)}</span><span class="metric-icon">${symbol}</span></div><strong>${esc(money(value))}</strong></article>`;
}
function shopRow(shop: Shop, index: number) {
  return `<article class="shop-row"><span class="rank">${String(index + 1).padStart(2, '0')}</span>
    <button class="shop-main" type="button" data-route="/shop/${encodeURIComponent(shop.id)}"><span class="shop-name">${esc(shop.name)}</span><span class="shop-meta">${esc(shop.overdueDays)} ${esc(t().daysLate)} <span aria-hidden="true">·</span> ${esc(shop.lastSale)}</span></button>
    <strong class="shop-balance">${esc(money(shop.owed))}</strong><span class="row-arrow" aria-hidden="true">${icon('arrow')}</span></article>`;
}

// ---------------------------------------------------------------------------
// Login screen
// ---------------------------------------------------------------------------
function renderLogin() {
  document.documentElement.lang = state.lang;
  document.documentElement.dir = 'ltr';
  app!.innerHTML = `<div class="app-shell login-shell">
    <header class="topbar">
      <span class="brand"><img src="/icons/icon.svg" alt="" width="32" height="32"><span>${esc(t().brand)}</span></span>
      <button class="lang-toggle" id="lang-toggle" type="button" aria-label="${esc(t().language)}">${state.lang === 'en' ? 'አማ' : 'EN'}</button>
    </header>
    <main id="main-content">
      <section class="form-card login-card">
        <h1>${esc(t().loginTitle)}</h1>
        <form id="login-form" novalidate>
          <label for="login-identifier">${esc(t().identifier)}</label>
          <input id="login-identifier" name="identifier" type="text" autocomplete="username" inputmode="email" required placeholder="${esc(t().identifier)}">
          <label for="login-password">${esc(t().password)}</label>
          <input id="login-password" name="password" type="password" autocomplete="current-password" required placeholder="••••••••">
          <p class="form-feedback" id="login-feedback" role="alert" aria-live="assertive"></p>
          <button class="button primary full" id="login-submit" type="submit">${esc(t().signIn)} ${icon('arrow')}</button>
        </form>
      </section>
    </main>
  </div>`;

  document.querySelector<HTMLButtonElement>('#lang-toggle')?.addEventListener('click', () => {
    state.lang = state.lang === 'en' ? 'am' : 'en';
    try { localStorage.setItem('dailyclose-lang', state.lang); } catch { /* ok */ }
    renderLogin();
  });

  document.querySelector<HTMLFormElement>('#login-form')?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = event.currentTarget as HTMLFormElement;
    const data = new FormData(form);
    const identifier = String(data.get('identifier') || '').trim();
    const password = String(data.get('password') || '');
    const feedback = document.querySelector<HTMLElement>('#login-feedback');
    const submit = document.querySelector<HTMLButtonElement>('#login-submit');
    if (!identifier || !password) return;
    if (submit) submit.disabled = true;
    if (feedback) feedback.textContent = '';
    try {
      const result = await api.login(identifier, password);
      state.user = result.user;
      render();
    } catch (error) {
      if (feedback) feedback.textContent = error instanceof Error ? error.message : t().loginError;
      if (submit) submit.disabled = false;
    }
  });
}

// ---------------------------------------------------------------------------
// App views
// ---------------------------------------------------------------------------
async function renderToday() {
  loading(t().today, t().todaySub);
  try {
    state.dashboard = await api.getDashboard();
    const dashboard = state.dashboard;
    const topShops = dashboard.topOverdue.slice(0, 5);
    shell(`<section class="metrics" aria-label="${esc(t().today)}">${metric(t().totalOwed, dashboard.totalOwed, 'feature')}${metric(t().overdueTotal, dashboard.overdueTotal, 'amber')}${metric(t().paidToday, dashboard.paidToday, 'blue')}</section>
      <section class="section-block"><div class="section-heading"><div><p class="eyebrow">${state.lang === 'en' ? 'NEEDS A FOLLOW-UP' : 'ክትትል የሚስፈልጉ'}</p><h2>${esc(t().topOverdue)}</h2></div><button class="inline-link" type="button" data-route="/overdue">${esc(t().overdue)} ${icon('arrow')}</button></div>
      <div class="shop-list">${topShops.length ? topShops.map(shopRow).join('') : `<div class="empty-inline">${esc(t().noShops)}</div>`}</div></section>
      <aside class="trust-note"><span class="trust-symbol">${icon('shield')}</span><p>${esc(t().overviewTip)}</p></aside>`, t().today, t().todaySub);
  } catch (error) { showError(message(error), renderToday, t().today); }
}
async function renderOverdue() {
  loading(t().overdue);
  try {
    state.shops = await api.getOverdueShops();
    const ordered = [...state.shops].sort((a, b) => b.overdueDays - a.overdueDays || b.owed - a.owed);
    const rows = ordered.map((shop, index) => {
      const phone = shop.phone.replace(/[^\d+]/g, '');
      const telegram = shop.telegram.replace(/^@/, '');
      return `<div class="follow-up-card">${shopRow(shop, index)}<div class="contact-actions"><a class="small-action" href="tel:${esc(phone)}">${icon('phone')}<span>${esc(t().call)}</span></a><a class="small-action" href="https://t.me/${encodeURIComponent(telegram)}" target="_blank" rel="noopener noreferrer">${icon('send')}<span>${esc(t().telegram)}</span></a></div></div>`;
    }).join('');
    shell(`<section class="section-block"><div class="intro-card"><span class="eyebrow">${esc(t().overdueKicker)}</span><p>${esc(t().overdueIntro)}</p></div><div class="shop-list">${rows || `<div class="empty-state"><h2>${esc(t().noShops)}</h2></div>`}</div></section>`, t().overdue, t().overdueIntro);
  } catch (error) { showError(message(error), renderOverdue, t().overdue); }
}
async function renderAddSale() {
  loading(t().addSale);
  try {
    if (!state.shops.length) state.shops = await api.getOverdueShops();
    const options = state.shops.map((shop) => `<option value="${esc(shop.id)}">${esc(shop.name)}</option>`).join('');
    shell(`<section class="form-card"><div class="form-intro"><span class="eyebrow">${esc(t().saleKicker)}</span><h2>${esc(t().saleTitle)}</h2><p>${esc(t().saleNote)}</p></div>
      <form id="sale-form" novalidate><label for="shop-select">${esc(t().shop)}</label><select name="shopId" id="shop-select" required><option value="">${esc(t().chooseShop)}</option>${options}</select>
      <label for="sale-amount">${esc(t().amount)}</label><div class="amount-input"><span>ETB</span><input name="amount" id="sale-amount" inputmode="decimal" type="number" min="0.01" step="0.01" placeholder="0.00" required></div>
      <fieldset class="due-field"><legend>${esc(t().due)}</legend><div class="day-pills">${[7, 14, 30].map((day) => `<label class="day-pill"><input type="radio" name="dueInDays" value="${day}" ${day === 14 ? 'checked' : ''}><span>${day} ${esc(t().days)}</span></label>`).join('')}</div></fieldset>
      <p class="form-feedback" id="sale-feedback" role="status" aria-live="polite"></p><button class="button primary full" id="sale-submit" type="submit">${esc(t().saveSale)} ${icon('arrow')}</button></form></section>`, t().addSale, t().saleNote);
    document.querySelector<HTMLFormElement>('#sale-form')?.addEventListener('submit', async (event) => {
      event.preventDefault(); const form = event.currentTarget as HTMLFormElement; const data = new FormData(form);
      const shopId = String(data.get('shopId') || ''); const amount = Number(data.get('amount')); const dueInDays = Number(data.get('dueInDays'));
      const feedback = document.querySelector<HTMLElement>('#sale-feedback'); const submit = document.querySelector<HTMLButtonElement>('#sale-submit');
      if (!shopId || !Number.isFinite(amount) || amount <= 0 || !dueInDays) {
        if (feedback) feedback.textContent = state.lang === 'en' ? 'Choose a shop and enter an amount greater than zero.' : 'ሱቅ ይምረጡ እና ከዜሮ በላይ የሆነ መጠን ያስገቡ።'; return;
      }
      if (submit) submit.disabled = true; if (feedback) feedback.textContent = '';
      try { await api.createSale({ shopId, amount, dueInDays }); if (feedback) feedback.textContent = t().saved; window.setTimeout(() => navigate('/'), 700); }
      catch (error) { if (feedback) feedback.textContent = message(error); if (submit) submit.disabled = false; }
    });
  } catch (error) { showError(message(error), renderAddSale, t().addSale); }
}
async function renderReview() {
  loading(t().review);
  try {
    state.reviews = await api.getReviewQueue();
    const cards = state.reviews.map(reviewCard).join('');
    shell(`<section class="section-block"><div class="intro-card"><span class="eyebrow">${esc(t().reviewKicker)}</span><p>${esc(t().reviewIntro)}</p></div><div class="review-list">${cards || `<div class="empty-state"><h2>${esc(t().noMatches)}</h2></div>`}</div></section>`, t().review, t().reviewIntro);
    document.querySelectorAll<HTMLButtonElement>('[data-review-action]').forEach((button) => button.addEventListener('click', async () => {
      const id = button.dataset.id || ''; button.disabled = true;
      try {
        if (button.dataset.reviewAction === 'approve') await api.approveReview(id); else await api.rejectReview(id);
        state.reviews = state.reviews.filter((item) => item.id !== id); renderReview();
      } catch (error) {
        const feedback = document.querySelector<HTMLElement>(`#review-feedback-${CSS.escape(id)}`);
        if (feedback) feedback.textContent = message(error); button.disabled = false;
      }
    }));
  } catch (error) { showError(message(error), renderReview, t().review); }
}
function reviewCard(item: ReviewItem) {
  const isVerified = item.verification === 'verified'; const id = esc(item.id);
  return `<article class="review-card"><div class="review-card-head"><div class="review-shop"><span class="shop-name">${esc(item.shopName)}</span><span class="shop-meta">${esc(item.submittedAt)} · ${esc(item.source)}</span></div><strong>${esc(money(item.amount))}</strong></div>
    <div class="review-statuses"><span class="status-chip ${isVerified ? 'verified' : 'unverified'}"><i></i>${isVerified ? esc(t().verified) : esc(t().unverified)}</span>${item.receiptPhotoOnFile ? `<span class="receipt-note">${icon('shield')}<span>${esc(t().receipt)}</span></span>` : ''}</div>
    <p class="form-feedback" id="review-feedback-${id}" role="status" aria-live="polite"></p><div class="review-actions"><button class="button quiet" type="button" data-review-action="reject" data-id="${id}">${esc(t().reject)}</button><button class="button primary" type="button" data-review-action="approve" data-id="${id}">${esc(t().approve)}</button></div></article>`;
}
function shopToken() { const segments = state.route.split('/'); try { return decodeURIComponent(segments[2] || ''); } catch { return segments[2] || ''; } }
async function renderShopBalance() {
  const token = shopToken(); loading(t().balance);
  try {
    state.balance = await api.getShopBalance(token); const balance = state.balance;
    shell(`<section class="public-banner">${icon('shield')}<span>${esc(t().public)}</span></section>
      <section class="balance-hero"><span class="eyebrow">${esc(t().balanceKicker)}</span><h2>${esc(balance.shopName)}</h2><strong>${esc(money(balance.owed))}</strong><p>${esc(t().updated)} ${esc(balance.updatedAt)}</p></section>
      <section class="balance-breakdown"><article><span>${esc(t().overdueTotal)}</span><strong>${esc(money(balance.overdue))}</strong></article><article><span>${esc(t().totalOwed)}</span><strong>${esc(money(balance.owed))}</strong></article></section>
      <section class="balance-actions"><button class="button primary full" id="confirm-balance" type="button">${icon('check')} ${esc(t().confirm)}</button><button class="button outline full" id="dispute-balance" type="button">! ${esc(t().dispute)}</button><p class="form-feedback" id="balance-feedback" role="status" aria-live="polite"></p></section><p class="balance-note">${esc(t().confirmNote)}</p>`, t().balance, t().public);
    document.querySelector<HTMLButtonElement>('#confirm-balance')?.addEventListener('click', () => balanceAction(token, 'confirm'));
    document.querySelector<HTMLButtonElement>('#dispute-balance')?.addEventListener('click', () => balanceAction(token, 'dispute'));
  } catch (error) { showError(message(error), renderShopBalance, t().balance); }
}
async function balanceAction(token: string, action: 'confirm' | 'dispute') {
  const button = document.querySelector<HTMLButtonElement>(action === 'confirm' ? '#confirm-balance' : '#dispute-balance');
  const feedback = document.querySelector<HTMLElement>('#balance-feedback'); if (!button) return; button.disabled = true;
  try {
    if (action === 'confirm') await api.confirmShopBalance(token); else await api.disputeShopBalance(token);
    if (feedback) feedback.textContent = action === 'confirm' ? t().actionSent : t().disputeSent;
  } catch (error) { if (feedback) feedback.textContent = message(error); button.disabled = false; }
}

// ---------------------------------------------------------------------------
// Router – public balance routes bypass auth entirely
// ---------------------------------------------------------------------------
function render() {
  if (state.route.startsWith('/shop/')) return void renderShopBalance();
  // Live mode: require login for all protected views
  if (!api.isAuthenticated()) return void renderLogin();
  if (state.route === '/add-sale') return void renderAddSale();
  if (state.route === '/overdue') return void renderOverdue();
  if (state.route === '/review') return void renderReview();
  return void renderToday();
}

// 401 from any API call → back to login
window.addEventListener('dc:unauthorized', () => {
  state.user = null;
  render();
});

window.addEventListener('hashchange', () => { state.route = currentRoute(); render(); });
render();
