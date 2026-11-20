"""
app/api/v1/schemas.py

Pydantic models for the /api/v1 JSON contract.  Field names are camelCase
where the PWA (daily-close-pwa/src/api.ts) expects camelCase.  All money
fields are ETB floats derived from integer santim at the response boundary.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class OkOut(BaseModel):
    ok: bool = True


class ShopOut(BaseModel):
    id: str
    name: str
    phone: str
    telegram: str
    owed: float
    overdueDays: int
    lastSale: str


class DashboardOut(BaseModel):
    totalOwed: float
    overdueTotal: float
    paidToday: float
    topOverdue: list[ShopOut]


class SaleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shopId: str = Field(min_length=1, max_length=64)
    amount: float = Field(gt=0, le=1_000_000_000, allow_inf_nan=False)
    dueInDays: int = Field(ge=0, le=365)


class ReviewItemOut(BaseModel):
    id: str
    shopName: str
    amount: float
    submittedAt: str
    verification: str  # 'verified' | 'unverified'
    receiptPhotoOnFile: bool
    source: str


class ApproveIn(BaseModel):
    """Optional body.  Approving a match whose amounts differ needs an explicit opt-in."""

    model_config = ConfigDict(extra="forbid")

    confirm_differing: bool = False


class BalanceOut(BaseModel):
    shopId: str
    shopName: str
    owed: float
    overdue: float
    updatedAt: str


class DisputeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason_code: str = Field(min_length=1, max_length=40)
    explanation: str = Field(min_length=1, max_length=1000)


# ---------------------------------------------------------------------------
# Auth schemas
# ---------------------------------------------------------------------------

class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identifier: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=1024)


class UserOut(BaseModel):
    id: str
    displayName: str
    role: str
    tenantId: str
    tenantName: str


class AuthOut(BaseModel):
    ok: bool = True
    token: str
    user: UserOut
