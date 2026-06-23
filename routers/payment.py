"""Payment/billing router — checkout, webhook, payment history.

Uses a mock payment provider that can be swapped for Stripe via config.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from db import get_db
from middleware.auth import get_current_user

router = APIRouter(prefix="/api/payment", tags=["payment"])

# ── Pricing ──────────────────────────────────────────────────────────────

PLAN_PRICES: dict[str, int] = {
    "basic": 999,       # $9.99
    "pro": 2999,        # $29.99
    "enterprise": 9999,  # $99.99
}

VALID_PLANS = frozenset(PLAN_PRICES.keys())

# ── Mock Payment Provider ────────────────────────────────────────────────


class MockPaymentProvider:
    """Mock payment provider — simulates Stripe checkout.

    Configurable: swap with a StripeProvider later by implementing the
    same interface (create_checkout_session, etc.).
    """

    def create_checkout_session(self, plan_type: str, amount_cents: int) -> dict:
        """Create a mock checkout session and return URLs."""
        checkout_id = str(uuid.uuid4())
        return {
            "checkout_id": checkout_id,
            "checkout_url": f"https://mock-pay.example/checkout/{checkout_id}",
        }


# Singleton — one provider instance across requests
payment_provider = MockPaymentProvider()

# ── Request / Response Models ────────────────────────────────────────────


class CreateCheckoutRequest(BaseModel):
    plan_type: str = Field(
        ..., description="Subscription plan: basic, pro, or enterprise"
    )


class CreateCheckoutResponse(BaseModel):
    checkout_id: str
    checkout_url: str
    plan_type: str
    amount_cents: int
    currency: str = "usd"
    status: str = "pending"


class WebhookRequest(BaseModel):
    checkout_id: str = Field(..., description="Checkout ID from create-checkout")
    status: Literal["completed", "failed"] = Field(..., description="Payment outcome")


class WebhookResponse(BaseModel):
    checkout_id: str
    status: str
    detail: str = ""


class PaymentRecord(BaseModel):
    checkout_id: str
    plan_type: str
    amount_cents: int
    currency: str
    status: str
    created_at: str


class PaymentHistoryResponse(BaseModel):
    payments: list[PaymentRecord]


# ── Endpoints ────────────────────────────────────────────────────────────


@router.post("/create-checkout", response_model=CreateCheckoutResponse)
async def create_checkout(
    body: CreateCheckoutRequest,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Create a checkout session for a subscription plan upgrade.

    Returns a mock checkout URL. In production this would redirect to Stripe.
    """
    if body.plan_type not in VALID_PLANS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Invalid plan '{body.plan_type}'. "
                f"Valid plans: {', '.join(sorted(VALID_PLANS))}"
            ),
        )

    amount_cents = PLAN_PRICES[body.plan_type]
    session = payment_provider.create_checkout_session(body.plan_type, amount_cents)

    sql = (
        "INSERT INTO payments "
        "(checkout_id, user_uuid, plan_type, amount_cents, currency, status) "
        "VALUES (?, ?, ?, ?, 'usd', 'pending')"
    )
    await db.execute(
        sql,
        (session["checkout_id"], current_user["sub"], body.plan_type, amount_cents),
    )
    await db.commit()

    return CreateCheckoutResponse(
        checkout_id=session["checkout_id"],
        checkout_url=session["checkout_url"],
        plan_type=body.plan_type,
        amount_cents=amount_cents,
        currency="usd",
        status="pending",
    )


@router.post("/webhook", response_model=WebhookResponse)
async def payment_webhook(
    body: WebhookRequest,
    db=Depends(get_db),
):
    """Mock webhook — simulates payment provider callback.

    When status=completed, the user's subscription is upgraded
    to the plan they paid for.
    """
    sql = (
        "SELECT checkout_id, user_uuid, plan_type, status "
        "FROM payments WHERE checkout_id = ?"
    )
    async with db.execute(sql, (body.checkout_id,),) as cursor:
        payment = await cursor.fetchone()

    if not payment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No payment found with checkout_id '{body.checkout_id}'",
        )

    if payment["status"] != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Payment already {payment['status']}",
        )

    new_status = body.status
    sql = (
        "UPDATE payments SET status = ?, "
        "updated_at = datetime('now') WHERE checkout_id = ?"
    )
    await db.execute(sql, (new_status, body.checkout_id),)

    # On successful payment, upgrade the user's subscription
    if new_status == "completed":
        plan_type = payment["plan_type"]
        user_uuid = payment["user_uuid"]

        sql = (
            "UPDATE users SET subscription_type = ?, "
            "updated_at = datetime('now') WHERE uuid = ?"
        )
        await db.execute(sql, (plan_type, user_uuid),)

        now = datetime.utcnow().isoformat()
        end_date = (datetime.utcnow() + timedelta(days=30)).isoformat()
        sql = (
            "INSERT INTO subscriptions "
            "(user_uuid, plan_type, status, start_date, end_date, auto_renew) "
            "VALUES (?, ?, 'active', ?, ?, 1) "
            "ON CONFLICT(user_uuid) DO UPDATE SET "
            "plan_type = excluded.plan_type, "
            "status = 'active', "
            "start_date = excluded.start_date, "
            "end_date = excluded.end_date, "
            "auto_renew = 1"
        )
        await db.execute(sql, (user_uuid, plan_type, now, end_date),)

    await db.commit()

    return WebhookResponse(
        checkout_id=body.checkout_id,
        status=new_status,
        detail=(
            "Payment processed successfully"
            if new_status == "completed" else "Payment failed"
        ),
    )


@router.get("/history", response_model=PaymentHistoryResponse)
async def payment_history(
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Get payment history for the current user."""
    async with db.execute(
        """SELECT checkout_id, plan_type, amount_cents, currency, status, created_at
           FROM payments WHERE user_uuid = ?
           ORDER BY created_at DESC""",
        (current_user["sub"],),
    ) as cursor:
        rows = await cursor.fetchall()

    return PaymentHistoryResponse(
        payments=[
            PaymentRecord(
                checkout_id=row["checkout_id"],
                plan_type=row["plan_type"],
                amount_cents=row["amount_cents"],
                currency=row["currency"],
                status=row["status"],
                created_at=row["created_at"],
            )
            for row in rows
        ]
    )
