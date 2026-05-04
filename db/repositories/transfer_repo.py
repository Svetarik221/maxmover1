from datetime import datetime

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Channel, PostMapping, TransferJob


class TransferRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_job(self, channel_id: int, total_posts: int) -> TransferJob:
        job = TransferJob(
            channel_id=channel_id,
            total_posts=total_posts,
            status="pending",
        )
        self.session.add(job)
        await self.session.commit()
        await self.session.refresh(job)
        return job

    async def start_job(self, job_id: int) -> None:
        job = await self.session.get(TransferJob, job_id)
        job.status = "running"
        job.started_at = datetime.utcnow()
        await self.session.commit()

    async def update_progress(self, job_id: int, transferred: int) -> None:
        job = await self.session.get(TransferJob, job_id)
        job.transferred_posts = transferred
        await self.session.commit()

    async def complete_job(self, job_id: int) -> None:
        job = await self.session.get(TransferJob, job_id)
        job.status = "done"
        job.completed_at = datetime.utcnow()
        await self.session.commit()

    async def fail_job(self, job_id: int, error: str) -> None:
        job = await self.session.get(TransferJob, job_id)
        job.status = "failed"
        job.error_message = error
        job.completed_at = datetime.utcnow()
        await self.session.commit()

    async def get_job(self, job_id: int) -> TransferJob | None:
        return await self.session.get(TransferJob, job_id)

    async def has_completed_free_transfer(self, channel_id: int) -> bool:
        """Проверяет, был ли уже завершённый бесплатный перенос для этого канала."""
        stmt = (
            select(func.count(TransferJob.id))
            .where(
                TransferJob.channel_id == channel_id,
                TransferJob.status == "done",
                TransferJob.transferred_posts > 0,
            )
        )
        result = await self.session.execute(stmt)
        return (result.scalar() or 0) > 0

    async def get_transferred_tg_ids(self, channel_id: int) -> set[int]:
        """Возвращает set tg_message_id, которые уже перенесены для канала."""
        stmt = select(PostMapping.tg_message_id).where(
            PostMapping.channel_id == channel_id
        )
        result = await self.session.execute(stmt)
        return {row[0] for row in result.all()}

    async def add_post_mapping(
        self, channel_id: int, tg_message_id: int, max_message_id: str
    ) -> None:
        # Проверяем, нет ли уже такого маппинга
        existing = await self.session.execute(
            select(PostMapping).where(
                PostMapping.channel_id == channel_id,
                PostMapping.tg_message_id == tg_message_id,
            )
        )
        if existing.scalar_one_or_none():
            return
        mapping = PostMapping(
            channel_id=channel_id,
            tg_message_id=tg_message_id,
            max_message_id=max_message_id,
        )
        self.session.add(mapping)
        await self.session.commit()
