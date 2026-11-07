"""Project shared file identity/path properties without reading note contents.

Vault mount prefixes are local presentation. path/folder values use canonical
share-relative paths on the wire and are translated by native clients. File
timestamps, sizes and dependency-bearing properties require other adapters.
"""

import posixpath
from dataclasses import dataclass

from app.services import base_group_values, bases_definition, managed_service

PROPERTIES = ("name", "basename", "fullname", "ext", "path", "folder", "file")


@dataclass(frozen=True)
class InventoryGroupValue:
    value: dict
    fingerprint: str
    references: frozenset[base_group_values.RecordIdentity]


def derive(property_name, source_path, identity):
    """Caller supplies currently authorized authoritative inventory, never a
    client path. Record lineage is required even for a plain string value.
    """
    managed_service.canonical_path(source_path)
    if not isinstance(property_name, str) or property_name.lower() not in PROPERTIES:
        raise base_group_values.UnavailableValue("Unsupported file property")
    property_name = property_name.lower()
    reference = identity.wire()
    name = posixpath.basename(source_path)
    dot = name.rfind(".")
    basename, extension = (name[:dot], name[dot + 1 :]) if dot >= 0 else (name, "")
    # Obsidian 1.13.7 getShortName hides only an exact lowercase .md suffix;
    # attachment names (and .MD) retain their extension. basename/fullname are
    # explicit alternatives in the native property adapter.
    values = {
        "name": basename if extension == "md" else name,
        "basename": basename,
        "fullname": name,
        "ext": extension,
        "path": source_path,
        "folder": posixpath.dirname(source_path),
    }
    value = (
        {"type": "file", **reference}
        if property_name == "file"
        else {"type": "text", "value": values[property_name]}
    )
    return InventoryGroupValue(
        value=value,
        fingerprint=bases_definition.fingerprint({"version": 1, "value": value}),
        references=frozenset({identity}),
    )
