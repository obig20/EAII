"""
app/api/v1/endpoints.py

Authenticated JSON REST adapter (/api/v1) over the existing domain services.

Invariants (see docs / task brief):
  Rule 1 — money stays integer santim; ETB floats appear only in responses.
  Rule 2 — evidence ladder: L4 can never be verified or approved.
  Rule 3 — set_tenant() is called as soon as a request is authenticated;
           public routes resolve the tenant solely from the signed token.
  Rule 4 — every state change appends an event (the domain functions emit
           invoice.created / retailer.balance_confirmed / retailer.balance_disputed;
           the review routes emit match.api_confirmed / match.api_rejected).
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.v1 import review
from app.api.v1.deps import ApiPrincipal, api_auth, api_owner, no_store, try_authenticate
from app.api.v1.formatting import etb_to_santim, format_timestamp, santim_to_etb
from app.api.v1.queries import build_dashboard, build_shop_list, compute_retailer_balance
from app.api.v1.schemas import (
    ApproveIn,
    BalanceOut,
    DashboardOut,
    DisputeIn,
    OkOut,
    ReviewItemOut,
    SaleIn,
    ShopOut,
)
from app.channels.web.routes import _get_client_ip
from app.kernel.db import get_session
from app.kernel.models import Retailer
from app.kernel.persistent_rate_limit import RateLimitExceededError, check_persistent_rate_limit
from app.kernel.retailer_confirmation import (
    DisputeReasonCode,
    TokenAlreadyUsedError,
    TokenError,
    TokenExpiredError,
    confirm_retailer_balance,
    dispute_retailer_balance,
    validate_confirmation_token,
)
from app.kernel.sales import DuplicateInvoiceError, InvalidSaleError, record_credit_sale
from app.kernel.tenant import set_tenant

router = APIRouter(prefix="/api/v1", tags=["api-v1"], dependencies=[Depends(no_store)])

_INVOICE_NUMBER_ATTEMPTS = 5
_PUBLIC_RATE_LIMIT = 15  # requests per window, per client IP
_PUBLIC_RATE_WINDOW_SECONDS = 60


def _parse_uuid(value: str, what: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"Invalid {what}.")


# ---------------------------------------------------------------------------
# A. Dashboard
# ---------------------------------------------------------------------------
@router.get("/dashboard/today", response_model=DashboardOut)
def dashboard_today(
    session: Session = Depends(get_session),
    principal: ApiPrincipal = Depends(api_auth),
):
    set_tenant(session, principal.tenant.id)
    return build_dashboard(session, principal.tenant.id, datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# B. Shops (all retailers, overdue first)
# ---------------------------------------------------------------------------
@router.get("/shops/overdue", response_model=list[ShopOut])
def shops_overdue(
    session: Session = Depends(get_session),
    principal: ApiPrincipal = Depends(api_auth),
):
    set_tenant(session, principal.tenant.id)
    return build_shop_list(session, principal.tenant.id, datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# C. Record a credit sale
# ---------------------------------------------------------------------------
@router.post("/sales", response_model=OkOut)
def create_sale(
    body: SaleIn,
    session: Session = Depends(get_session),
    principal: ApiPrincipal = Depends(api_auth),
):
    tenant, user = principal.tenant, principal.user
    set_tenant(session, tenant.id)

    retailer_id = _parse_uuid(body.shopId, "shopId")
    try:
        amount_santim = etb_to_santim(body.amount)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    shop = session.execute(
        select(Retailer.id).where(Retailer.id == retailer_id, Retailer.tenant_id == tenant.id)
    ).scalar_one_or_none()
    if shop is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Shop not found.")

    today = datetime.now(timezone.utc).date()
    due_date = today + timedelta(days=body.dueInDays)

    for attempt in range(_INVOICE_NUMBER_ATTEMPTS):
        invoice_number = f"INV-{today:%y%m%d}-{secrets.token_hex(3).upper()}"
        try:
            record_credit_sale(  # emits the append-only invoice.created event
                session,
                tenant_id=tenant.id,
                user_id=user.id,
                invoice_number=invoice_number,
                retailer_id=retailer_id,
                amount_santim=amount_santim,
                issued_date=today,
                due_date=due_date,
            )
            break
        except DuplicateInvoiceError:
            if attempt == _INVOICE_NUMBER_ATTEMPTS - 1:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    detail="Could not allocate a unique invoice number. Please try again.",
                )
        except (InvalidSaleError, ValueError, TypeError) as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"Invalid sale: {exc}")

    session.commit()
    return OkOut()


# ---------------------------------------------------------------------------
# D / E / F. Review queue
# ---------------------------------------------------------------------------
@router.get("/review-queue", response_model=list[ReviewItemOut])
def review_queue(
    session: Session = Depends(get_session),
    principal: ApiPrincipal = Depends(api_auth),
):
    set_tenant(session, principal.tenant.id)
    return review.list_review_items(session, principal.tenant.id, datetime.now(timezone.utc))


@router.post("/review-queue/{item_id}/approve", response_model=OkOut)
def approve_review_item(
    item_id: str,
    body: ApproveIn | None = None,
    session: Session = Depends(get_session),
    principal: ApiPrincipal = Depends(api_owner),
):
    set_tenant(session, principal.tenant.id)
    review.approve_item(
        session,
        tenant=principal.tenant,
        user=principal.user,
        item_id=_parse_uuid(item_id, "review item id"),
        confirm_differing=bool(body and body.confirm_differing),
    )
    session.commit()
    return OkOut()


@router.post("/review-queue/{item_id}/reject", response_model=OkOut)
def reject_review_item(
    item_id: str,
    session: Session = Depends(get_session),
    principal: ApiPrincipal = Depends(api_owner),
):
    set_tenant(session, principal.tenant.id)
    review.reject_item(
        session,
        tenant=principal.tenant,
        user=principal.user,
        item_id=_parse_uuid(item_id, "review item id"),
    )
    session.commit()
    return OkOut()


# ---------------------------------------------------------------------------
# G. Public balance confirmation (no login; rate-limited by client IP)
# ---------------------------------------------------------------------------
def _public_rate_limit(request: Request, session: Session) -> None:
    client_ip = _get_client_ip(request)
    try:
        check_persistent_rate_limit(
            session,
            key=f"api_public:ip:{client_ip}",
            max_requests=_PUBLIC_RATE_LIMIT,
            window_seconds=_PUBLIC_RATE_WINDOW_SECONDS,
        )
    except RateLimitExceededError as exc:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
            headers={"Retry-After": str(_PUBLIC_RATE_WINDOW_SECONDS)},
        )
    # Persist the attempt now so invalid-token requests still count toward the limit.
    session.commit()


def _token_http_error(exc: TokenError) -> HTTPException:
    if isinstance(exc, TokenExpiredError):
        return HTTPException(status.HTTP_410_GONE, detail=str(exc))
    if isinstance(exc, TokenAlreadyUsedError):
        return HTTPException(status.HTTP_409_CONFLICT, detail=str(exc))
    return HTTPException(status.HTTP_404_NOT_FOUND, detail="Balance link not found.")


def _resolve_token_for_update(session: Session, raw_token: str):
    """Validate the token (establishes tenant context), then lock it against concurrent use."""
    try:
        token_rec, retailer, _ = validate_confirmation_token(session, raw_token=raw_token)
    except TokenError as exc:
        raise _token_http_error(exc)
    set_tenant(session, token_rec.tenant_id)
    session.refresh(token_rec, with_for_update=True)
    if token_rec.used_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="This confirmation link was already completed.")
    return token_rec, retailer


def _looks_like_uuid(value: str) -> bool:
    if len(value) not in (32, 36):
        return False
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


@router.get("/public/balances/{raw_token}", response_model=BalanceOut)
def public_balance(
    raw_token: str,
    request: Request,
    session: Session = Depends(get_session),
):
    _public_rate_limit(request, session)
    now = datetime.now(timezone.utc)

    if _looks_like_uuid(raw_token):
        # In-app preview by retailer id.  Tenant context can only come from a logged-in user:
        # an unauthenticated retailer-id lookup would expose balances across tenants (Rule 3),
        # so anonymous callers get the same 404 as an unknown link.
        principal = try_authenticate(request, session)
        if principal is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Balance link not found.")
        tenant_id = principal.tenant.id
        set_tenant(session, tenant_id)
        retailer = session.execute(
            select(Retailer).where(Retailer.id == uuid.UUID(raw_token), Retailer.tenant_id == tenant_id)
        ).scalar_one_or_none()
        if retailer is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Balance link not found.")
    else:
        try:
            token_rec, retailer, _ = validate_confirmation_token(session, raw_token=raw_token)
        except TokenError as exc:
            raise _token_http_error(exc)
        tenant_id = token_rec.tenant_id
        set_tenant(session, tenant_id)

    owed, overdue = compute_retailer_balance(
        session, tenant_id=tenant_id, retailer_id=retailer.id, as_of=now.date()
    )
    return BalanceOut(
        shopId=str(retailer.id),
        shopName=retailer.name,
        owed=santim_to_etb(owed),
        overdue=santim_to_etb(overdue),
        updatedAt=format_timestamp(now, now),
    )


@router.post("/public/balances/{raw_token}/confirm", response_model=OkOut)
def public_confirm_balance(
    raw_token: str,
    request: Request,
    session: Session = Depends(get_session),
):
    _public_rate_limit(request, session)
    token_rec, _retailer = _resolve_token_for_update(session, raw_token)

    # Rule 2: no explicit payment details are accepted here, so confirming a balance
    # can never create payment evidence — it only records the confirmation event.
    confirm_retailer_balance(session, token_record=token_rec, explicit_payments=None)
    session.commit()
    return OkOut()


@router.post("/public/balances/{raw_token}/dispute", response_model=OkOut)
def public_dispute_balance(
    raw_token: str,
    body: DisputeIn,
    request: Request,
    session: Session = Depends(get_session),
):
    _public_rate_limit(request, session)

    try:
        reason = DisputeReasonCode(body.reason_code.strip().upper())
    except ValueError:
        allowed = ", ".join(c.value for c in DisputeReasonCode)
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"reason_code must be one of: {allowed}.",
        )
    explanation = body.explanation.strip()
    if not explanation:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="explanation is required.")

    token_rec, _retailer = _resolve_token_for_update(session, raw_token)
    try:
        dispute_retailer_balance(
            session,
            token_record=token_rec,
            reason_code=reason.value,
            explanation=explanation,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    session.commit()
    return OkOut()
