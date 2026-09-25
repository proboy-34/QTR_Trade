from typing import Any

from sqlalchemy.inspection import inspect


def serialize(obj: Any) -> dict[str, Any]:
    return {column.key: getattr(obj, column.key) for column in inspect(obj).mapper.column_attrs}
