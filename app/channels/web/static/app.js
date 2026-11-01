/**
 * Daily Close Client Application Scripts
 * Birana Technology — Wholesale & Distribution Credit Management
 * 
 * Strict CSP Compliant:
 * - No inline scripts or event handlers (no on* attributes)
 * - Safe event delegation via data-* attributes
 * - No user data evaluated via eval or Function constructor
 */

(function() {
  'use strict';

  // --- 1. Review Queue State & Navigation ---
  function getCardsWrapper() {
    return document.getElementById('review-cards-wrapper');
  }

  function initReviewQueue() {
    var wrapper = getCardsWrapper();
    if (!wrapper) return;
    wrapper.classList.add('has-js');

    var cards = wrapper.querySelectorAll('.match-card');
    if (cards.length > 0) {
      var hasActive = wrapper.querySelector('.match-card.is-active');
      if (!hasActive) {
        showMatchCardByIndex(0);
      }
    }
  }

  window.showMatchCardByIndex = function(targetIdx) {
    var wrapper = getCardsWrapper();
    if (!wrapper) return;
    var cards = wrapper.querySelectorAll('.match-card');
    if (!cards.length) return;

    if (targetIdx < 0) targetIdx = 0;
    if (targetIdx >= cards.length) targetIdx = cards.length - 1;

    cards.forEach(function(card, idx) {
      if (idx === targetIdx) {
        card.classList.add('is-active');
        card.style.display = '';
      } else {
        card.classList.remove('is-active');
        card.style.display = 'none';
      }
    });

    var items = document.querySelectorAll('.queue-nav-item');
    items.forEach(function(item, idx) {
      if (idx === targetIdx) {
        item.classList.add('is-active');
      } else {
        item.classList.remove('is-active');
      }
    });
  };

  window.nextMatchCard = function() {
    var wrapper = getCardsWrapper();
    if (!wrapper) return;
    var cards = Array.from(wrapper.querySelectorAll('.match-card'));
    var currentIdx = cards.findIndex(function(c) { return c.classList.contains('is-active'); });
    if (currentIdx === -1) currentIdx = 0;
    if (currentIdx < cards.length - 1) {
      window.showMatchCardByIndex(currentIdx + 1);
    }
  };

  window.prevMatchCard = function() {
    var wrapper = getCardsWrapper();
    if (!wrapper) return;
    var cards = Array.from(wrapper.querySelectorAll('.match-card'));
    var currentIdx = cards.findIndex(function(c) { return c.classList.contains('is-active'); });
    if (currentIdx === -1) currentIdx = 0;
    if (currentIdx > 0) {
      window.showMatchCardByIndex(currentIdx - 1);
    }
  };

  window.skipMatchCard = function(cardId) {
    var el = document.getElementById('match-card-' + cardId);
    if (!el || !el.parentNode) return;
    el.parentNode.appendChild(el);
    window.showMatchCardByIndex(0);
  };

  // --- 2. Toast Notifications ---
  window.showToast = function(message, type, undoCallback) {
    var container = document.getElementById('toast-container');
    if (!container) return;

    var toast = document.createElement('div');
    toast.className = 'toast ' + (type === 'danger' ? 'toast-danger' : 'toast-success');
    toast.setAttribute('role', 'status');

    var span = document.createElement('span');
    span.textContent = message;
    toast.appendChild(span);

    if (undoCallback) {
      var undoBtn = document.createElement('button');
      undoBtn.type = 'button';
      undoBtn.className = 'toast-undo-btn';
      undoBtn.textContent = 'Undo';
      undoBtn.addEventListener('click', function() {
        undoCallback();
        toast.remove();
      });
      toast.appendChild(undoBtn);
    }

    var closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.className = 'toast-close-btn';
    closeBtn.setAttribute('aria-label', 'Dismiss');
    closeBtn.innerHTML = '&times;';
    closeBtn.addEventListener('click', function() {
      toast.remove();
    });
    toast.appendChild(closeBtn);

    container.appendChild(toast);
    setTimeout(function() {
      if (toast.parentNode) toast.remove();
    }, 10000);
  };

  // --- 3. Event Delegation for Data Attributes ---
  document.addEventListener('click', function(e) {
    // A. Toggle Drawer
    var toggleBtn = e.target.closest('[data-action="toggle-drawer"]');
    if (toggleBtn) {
      var targetId = toggleBtn.getAttribute('data-target');
      var drawer = document.getElementById(targetId);
      if (drawer) {
        drawer.style.display = drawer.style.display === 'none' ? 'block' : 'none';
      }
      return;
    }

    // B. Close Drawer
    var closeBtn = e.target.closest('[data-action="close-drawer"]');
    if (closeBtn) {
      var closeTargetId = closeBtn.getAttribute('data-target');
      var closeDrawer = document.getElementById(closeTargetId);
      if (closeDrawer) {
        closeDrawer.style.display = 'none';
      }
      return;
    }

    // C. Password Visibility Toggle
    var pwdBtn = e.target.closest('[data-action="toggle-password"]');
    if (pwdBtn) {
      var inputId = pwdBtn.getAttribute('data-target');
      var input = document.getElementById(inputId);
      if (input) {
        if (input.type === 'password') {
          input.type = 'text';
          pwdBtn.textContent = 'Hide';
        } else {
          input.type = 'password';
          pwdBtn.textContent = 'Show';
        }
      }
      return;
    }

    // D. Review Queue Navigation Actions
    var cardNav = e.target.closest('[data-action="show-card"]');
    if (cardNav) {
      var idx = parseInt(cardNav.getAttribute('data-index'), 10);
      if (!isNaN(idx)) {
        window.showMatchCardByIndex(idx);
      }
      return;
    }

    var prevBtn = e.target.closest('[data-action="prev-card"]');
    if (prevBtn) {
      window.prevMatchCard();
      return;
    }

    var nextBtn = e.target.closest('[data-action="next-card"]');
    if (nextBtn) {
      window.nextMatchCard();
      return;
    }

    var skipBtn = e.target.closest('[data-action="skip-card"]');
    if (skipBtn) {
      var cardId = skipBtn.getAttribute('data-card-id');
      if (cardId) {
        window.skipMatchCard(cardId);
      }
      return;
    }

    // E. Copy Link
    var copyBtn = e.target.closest('[data-action="copy-link"]');
    if (copyBtn) {
      var copyTargetId = copyBtn.getAttribute('data-target');
      var copyInput = document.getElementById(copyTargetId);
      if (copyInput) {
        copyInput.select();
        try {
          navigator.clipboard.writeText(copyInput.value);
          copyBtn.textContent = 'Copied!';
          setTimeout(function() { copyBtn.textContent = 'Copy Link'; }, 2000);
        } catch (err) {
          document.execCommand('copy');
        }
      }
      return;
    }

    // F. Confirm Differing Match
    var diffBtn = e.target.closest('[data-confirm-differing]');
    if (diffBtn) {
      var promptMsg = diffBtn.getAttribute('data-confirm-prompt');
      if (!promptMsg) {
        var retailer = diffBtn.getAttribute('data-retailer') || '';
        var invoice = diffBtn.getAttribute('data-invoice') || '';
        var ref = diffBtn.getAttribute('data-ref') || 'None';
        var credit = diffBtn.getAttribute('data-credit') || '';

        promptMsg = 'Confirm and keep ' + credit + ' as credit?\n\n' +
          '• Retailer: ' + retailer + '\n' +
          '• Invoice: ' + invoice + '\n' +
          '• Raw Reference: ' + ref + '\n\n' +
          'Remaining amount will be kept on file as credit and not applied to other invoices.';
      }

      if (!window.confirm(promptMsg)) {
        e.preventDefault();
        e.stopPropagation();
      }
      return;
    }

    // G. Confirm Reject Match
    var rejBtn = e.target.closest('[data-confirm-reject]');
    if (rejBtn) {
      var rejMsg = rejBtn.getAttribute('data-confirm-prompt') || 'Reject this suggested match? The payment line will remain in audit records.';
      if (!window.confirm(rejMsg)) {
        e.preventDefault();
        e.stopPropagation();
      }
      return;
    }
  });

  // --- 4. Form Submit Interceptions for Confirmations ---
  document.addEventListener('submit', function(e) {
    // Reverse Match Confirmation
    if (e.target.matches('[data-confirm="reverse-match"]')) {
      var revMsg = 'Are you sure you want to reverse this match? This will restore open balances and log an audit reversal event.';
      if (!window.confirm(revMsg)) {
        e.preventDefault();
        e.stopPropagation();
      }
      return;
    }

    // User Management Confirmations
    if (e.target.matches('[data-confirm-action="deactivate-user"]')) {
      var targetName = e.target.getAttribute('data-display-name') || 'this user';
      if (!window.confirm('Deactivate account for ' + targetName + '?')) {
        e.preventDefault();
        e.stopPropagation();
      }
      return;
    }

    if (e.target.matches('[data-confirm-action="reset-link"]')) {
      var resetName = e.target.getAttribute('data-display-name') || 'this user';
      if (!window.confirm('Generate a single-use 24-hour password reset link for ' + resetName + '?')) {
        e.preventDefault();
        e.stopPropagation();
      }
      return;
    }
  });

  // --- 5. Lifecycle Initialization ---
  document.addEventListener('DOMContentLoaded', function() {
    initReviewQueue();
  });

  document.body.addEventListener('htmx:afterSwap', function(evt) {
    var wrapper = getCardsWrapper();
    if (!wrapper) return;
    var remaining = wrapper.querySelectorAll('.match-card');
    if (remaining.length > 0) {
      var hasActive = wrapper.querySelector('.match-card.is-active');
      if (!hasActive) {
        remaining[0].classList.add('is-active');
        remaining[0].style.display = '';
      }
      window.showMatchCardByIndex(0);
    }
  });

})();
