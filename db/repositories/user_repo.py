from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import User


class UserRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_or_create(
        self, tg_user_id: int, username: str | None = None, first_name: str | None = None
    ) -> User:
        stmt = select(User).where(User.tg_user_id == tg_user_id)
        result = await self.session.execute(stmt)
        user = result.scalar_one_or_none()
        if user is None:
            user = User(
                tg_user_id=tg_user_id, username=username, first_name=first_name
            )
            self.session.add(user)
            await self.session.commit()
            await self.session.refresh(user)
        return user

    async def get_by_tg_id(self, tg_user_id: int) -> User | None:
        stmt = select(User).where(User.tg_user_id == tg_user_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def update_tariff(self, user_id: int, tariff: str) -> None:
        stmt = select(User).where(User.id == user_id)
        result = await self.session.execute(stmt)
        user = result.scalar_one()
        user.tariff = tariff
        await self.session.commit()

    async def update_timezone(self, user_id: int, offset: int) -> None:
        stmt = select(User).where(User.id == user_id)
        result = await self.session.execute(stmt)
        user = result.scalar_one()
        user.timezone_offset = offset
        await self.session.commit()
