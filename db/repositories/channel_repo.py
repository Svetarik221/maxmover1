from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Channel


class ChannelRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        user_id: int,
        tg_channel_username: str,
        verification_code: str,
    ) -> Channel:
        channel = Channel(
            user_id=user_id,
            tg_channel_username=tg_channel_username,
            verification_code=verification_code,
        )
        self.session.add(channel)
        await self.session.commit()
        await self.session.refresh(channel)
        return channel

    async def get_by_id(self, channel_id: int) -> Channel | None:
        return await self.session.get(Channel, channel_id)

    async def get_by_user_and_tg(self, user_id: int, tg_username: str) -> Channel | None:
        stmt = select(Channel).where(
            Channel.user_id == user_id,
            Channel.tg_channel_username == tg_username,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def verify(self, channel_id: int, tg_chat_id: int | None = None) -> None:
        channel = await self.session.get(Channel, channel_id)
        channel.is_verified = True
        if tg_chat_id is not None:
            channel.tg_channel_id = tg_chat_id
        await self.session.commit()

    async def set_max_channel(
        self, channel_id: int, max_channel_url: str, max_channel_id: str
    ) -> None:
        channel = await self.session.get(Channel, channel_id)
        channel.max_channel_url = max_channel_url
        channel.max_channel_id = max_channel_id
        await self.session.commit()
