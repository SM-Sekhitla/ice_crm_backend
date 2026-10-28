"""Mapped CSV rows are validated as JSON; imports never overwrite existing people."""

import csv
import io

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.crm import contact_record, create_contact
from app.api.deps import Session, require
from app.api.schemas import BulkDelete, BulkTags, ContactImport
from app.db.models import Contact
from app.services.common import audit

router = APIRouter(prefix="/contacts", tags=["Contact import and export"])


@router.post("/import")
async def import_contacts(body: ContactImport, session: Session, user=Depends(require("contacts.create"))):
    imported, skipped = [], []
    for index, row in enumerate(body.rows):
        try:
            # Savepoints isolate duplicate rows; authorization/validation failures
            # abort the entire transaction rather than silently expanding access.
            async with session.begin_nested():
                record = await create_contact(row, session, user)
            imported.append(record["id"])
        except IntegrityError:
            skipped.append({"row": index + 1, "reason": "Duplicate or protected record"})
    audit(session, user, f"Imported {len(imported)} contacts; skipped {len(skipped)} rows")
    return {"importedIds": imported, "skipped": skipped}


@router.post("/bulk-tags")
async def bulk_tags(body: BulkTags, session: Session, user=Depends(require("contacts.edit"))):
    for cid in sorted(set(body.contact_ids)):
        record = await contact_record(session, user, cid)
        # Explicit row lock prevents lost updates between simultaneous edits.
        record = await session.scalar(select(Contact).where(Contact.id == cid).with_for_update())
        tags = list(dict.fromkeys([*record.tags, *body.tags]))
        if len(tags) > 30:
            raise HTTPException(422, "A contact cannot have more than 30 tags")
        record.tags = tags
        audit(session, user, "Added contact tags", cid)
    return {"updated": len(set(body.contact_ids))}


@router.get("/export")
async def export_contacts(session: Session, user=Depends(require("contacts.view"))):
    query = select(Contact).order_by(Contact.email).limit(10000)
    output = io.StringIO()
    writer = csv.writer(output)
    fields = ["first_name", "last_name", "email", "company", "phone", "consent", "do_not_email"]
    writer.writerow(fields)
    for record in await session.scalars(query):
        # Prevent spreadsheet formula injection on exported user input.
        values = [str(getattr(record, field)) for field in fields]
        writer.writerow(
            ["'" + value if value.startswith(("=", "+", "-", "@", "\t", "\r")) else value for value in values]
        )
    return Response(
        output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="contacts.csv"'},
    )


@router.post("/bulk-delete", status_code=204)
async def delete_contacts(body: BulkDelete, session: Session, user=Depends(require("contacts.delete"))):
    records = []
    for cid in sorted(set(body.contact_ids)):
        records.append(await contact_record(session, user, cid))
    for record in records:
        audit(session, user, "Deleted contact and linked lifecycle records")
        await session.delete(record)
