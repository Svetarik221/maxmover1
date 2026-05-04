from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Subscription


class SubscriptionRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        user_id: int,
        tariff: str,
        payment_id: str,
        amount: float,
        starts_at: datetime,
        expires_at: datetime | None = None,
        channel_id: int | None = None,
    ) -> Subscription:
        sub = Subscription(
            user_id=user_id,
            tariff=tariff,
            payment_id=payment_id,
            amount=amount,
            starts_at=starts_at,
            expires_at=expires_at,
            channel_id=channel_id,
            status="active",
        )
        self.session.add(sub)
        await self.session.commit()
        await self.session.refresh(sub)
        return sub

    async def get_active(self, user_id: int) -> Subscription | None:
        stmt = (
            select(Subscription)
            .where(
                Subscription.user_id == user_id,
                Subscription.status == "active",
            )
            .order_by(Subscription.created_at.desc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_active_for_channel(self, user_id: int, channel_id: int) -> Subscription | None:
        """Активная подписка Старт для конкретного канала."""
        stmt = (
            select(Subscription)
            .where(
                Subscription.user_id == user_id,
                Subscription.channel_id == channel_id,
                Subscription.status == "active",
            )
            .order_by(Subscription.created_at.desc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()
