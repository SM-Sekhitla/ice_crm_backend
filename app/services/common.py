from pydantic.alias_generators import to_camel
from sqlalchemy import inspect

from app.db.models import Activity


def serialize(record):
    return {
        to_camel(col.key): getattr(record, col.key)
        for col in inspect(record).mapper.column_attrs
        if col.key not in {"password_hash", "token_version", "digest", "encrypted_token"}
    }


def audit(session, user, description, contact_id=None, product_id=None):
    session.add(
        Activity(user_id=user.id, description=description, contact_id=contact_id, product_id=product_id)
    )
