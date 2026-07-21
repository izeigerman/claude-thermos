import hashlib
import json

_HASH_LENGTH = 8


def extract_session_id(request_body: dict) -> str | None:
    """Return metadata.user_id's embedded session_id, or None if absent/unparseable.
    request_body['metadata']['user_id'] is a JSON string containing {"session_id": ...}."""
    metadata = request_body.get("metadata")
    if not isinstance(metadata, dict):
        return None
    user_id = metadata.get("user_id")
    if not isinstance(user_id, str):
        return None
    try:
        decoded = json.loads(user_id)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(decoded, dict):
        return None
    session_id = decoded.get("session_id")
    if not isinstance(session_id, str):
        return None
    return session_id


def _system_text(system: str | list[dict] | None) -> str:
    if system is None:
        return ""
    if isinstance(system, str):
        return system
    return "".join(block.get("text", "") for block in system)


class LineageId(str):
    """Identifier for a cacheable prefix lineage.

    A distinct type (a ``str`` subclass) so it type-checks apart from
    arbitrary strings, while remaining usable as a dict key and serializing
    straight to JSON. The value is a short hex digest of the prefix identity:
    model + sorted tool names + concatenated system text."""

    __slots__ = ()

    @classmethod
    def from_request_body(cls, request_body: dict) -> "LineageId":
        """Derive the lineage id from a request body. Two requests with the
        same model, tool-name set, and system text produce equal ids; a
        different model, tool set, or system text produces a different one.
        Tool ordering does not affect the result."""
        model = request_body.get("model", "")
        tool_names = sorted(tool.get("name", "") for tool in request_body.get("tools", []))
        system_text = _system_text(request_body.get("system"))
        identity = json.dumps([model, tool_names, system_text])
        digest = hashlib.sha256(identity.encode()).hexdigest()[:_HASH_LENGTH]
        return cls(digest)
