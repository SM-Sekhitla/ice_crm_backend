"""Only explicitly published image bytes are public; signatures remain protected."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import EmailImage
from app.db.session import get_session

router = APIRouter(tags=["Email images"])


@router.get("/email-images/{image_id}")
async def public_image(
    image_id: Annotated[str, Path(pattern=r"^[a-f0-9]{64}$")],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    image = await session.get(EmailImage, image_id)
    if not image:
        raise HTTPException(404, "Image not found")
    return Response(
        content=image.data,
        media_type=image.content_type,
        headers={"Cache-Control": "public, max-age=31536000, immutable",
                 "X-Content-Type-Options": "nosniff",
                 "Content-Disposition": "inline"},
    )
