"""Bounded Bases YAML inspection. Never evaluate filters, formulas or plugin code."""

import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import dataclass

import yaml

MAX_BYTES = 256 * 1024
MAX_VIEWS = 64


class InvalidBase(ValueError):
    pass


class DefinitionLoader(yaml.SafeLoader):
    """Bound composition before allocation; reject aliases, tags and duplicate keys.

    Core plain scalars use JSON-like booleans/numbers. In particular a view
    name of 'On', or a calendar date, must not become a YAML 1.1 bool/date.
    Native saved definitions don't need aliases, merge keys or custom tags.
    """

    yaml_implicit_resolvers = {}

    def __init__(self, stream):
        super().__init__(stream)
        self.depth = 0
        self.nodes = 0

    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise InvalidBase("Aliases are not supported in shared Bases definitions")
        self.depth += 1
        self.nodes += 1
        if self.depth > 32 or self.nodes > 20000:
            raise InvalidBase("Bases definition exceeds structural limits")
        try:
            return super().compose_node(parent, index)
        finally:
            self.depth -= 1

    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=True)
            if not isinstance(key, str) or key == "<<" or key in result:
                raise InvalidBase("Bases mappings require unique string keys")
            result[key] = self.construct_object(value_node, deep=True)
        return result


for tag, pattern, first in (
    ("null", r"^(?:~|null|Null|NULL|)$", ["~", "n", "N", ""]),
    ("bool", r"^(?:true|True|TRUE|false|False|FALSE)$", "tTfF"),
    ("int", r"^[-+]?(?:0|[1-9][0-9]*)$", "-+0123456789"),
    ("float", r"^[-+]?(?:[0-9]+\.[0-9]*|\.[0-9]+|[0-9]+)(?:[eE][-+]?[0-9]+)?$", "-+.0123456789"),
):
    DefinitionLoader.add_implicit_resolver("tag:yaml.org,2002:" + tag, re.compile(pattern), first)

# SafeLoader has constructors for timestamps/sets/binary as well. Reject those
# and explicit application tags instead of converting to non-JSON values.
DefinitionLoader.yaml_constructors = {
    tag: constructor
    for tag, constructor in yaml.SafeLoader.yaml_constructors.items()
    if tag
    in {
        "tag:yaml.org,2002:" + name
        for name in ("null", "bool", "int", "float", "str", "seq", "map")
    }
}
DefinitionLoader.add_constructor(None, yaml.SafeLoader.construct_undefined)


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Definition:
    sha256: str
    value: dict
    views: list[dict]


def parse(raw: bytes) -> Definition:
    if not raw or len(raw) > MAX_BYTES:
        raise InvalidBase("Bases definition exceeds the byte limit or is empty")
    try:
        value = yaml.load(raw.decode("utf-8-sig"), Loader=DefinitionLoader)
        if not isinstance(value, dict) or not isinstance(value.get("views"), list):
            raise InvalidBase("Bases definition must contain a views list")
        views = value["views"]
        if len(views) > MAX_VIEWS:
            raise InvalidBase("Bases definition contains too many views")
        names = set()
        for view in views:
            if not isinstance(view, dict):
                raise InvalidBase("Invalid saved view")
            for field in ("name", "type"):
                text = view.get(field)
                if (
                    not isinstance(text, str)
                    or not text
                    or len(text) > 256
                    or any(ord(c) < 32 for c in text)
                ):
                    raise InvalidBase("Saved views require a bounded name and type")
            if view["name"] in names:
                raise InvalidBase("Saved view names must be unique")
            names.add(view["name"])

        # Reject non-finite/oversized numeric values, surrogate escapes and
        # non-JSON values without putting private YAML into an API error.
        def check(item):
            if isinstance(item, dict):
                for k, v in item.items():
                    k.encode("utf-8")
                    check(v)
            elif isinstance(item, list):
                for v in item:
                    check(v)
            elif isinstance(item, str):
                item.encode("utf-8")
            elif isinstance(item, float) and not math.isfinite(item):
                raise InvalidBase("Invalid number in Bases definition")
            elif isinstance(item, int) and abs(item) > 9007199254740991:
                raise InvalidBase("Number exceeds the shared Bases precision limit")

        check(value)
        canonical(value).encode("utf-8")
        return Definition(hashlib.sha256(raw).hexdigest(), value, views)
    except (yaml.YAMLError, ValueError, TypeError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, InvalidBase):
            raise
        raise InvalidBase("Invalid or unsupported Bases definition") from None


def reconcile(
    previous: list[dict], current: list[dict]
) -> tuple[dict[int, str], list[str], list[str]]:
    """Match only unambiguous active identities; indexes locate this snapshot only.

    Exact descriptors survive reorder. Unique unchanged names survive property
    or layout changes. A pure rename matches a unique name-free descriptor.
    Any remaining old/new combination is ambiguous and requires editor repair.
    Already-unmatched/archived identities never silently reappear.
    """
    original = {item["id"]: item["descriptor"] for item in previous if item["state"] == "active"}
    old = dict(original)
    new = dict(enumerate(current))
    bindings = {}
    for step, key in enumerate(
        (
            fingerprint,
            lambda view: view["name"],
            lambda view: fingerprint({k: v for k, v in view.items() if k != "name"}),
        )
    ):
        old_keys, new_keys = {}, {}
        for view_id, view in old.items():
            old_keys.setdefault(key(view), []).append(view_id)
        for slot, view in new.items():
            new_keys.setdefault(key(view), []).append(slot)
        original_counts = Counter(key(view) for view in original.values()) if step == 2 else {}
        current_counts = Counter(key(view) for view in current) if step == 2 else {}
        for value, old_ids in old_keys.items():
            slots = new_keys.get(value, [])
            if len(old_ids) == len(slots) == 1:
                # A disappearance plus a copy of another identical view is
                # indistinguishable from rename. Count all descriptors, even
                # already-matched ones, before inferring a pure rename.
                if step == 2 and (original_counts[value] != 1 or current_counts[value] != 1):
                    continue
                view_id, slot = old_ids[0], slots[0]
                bindings[slot] = view_id
                del old[view_id]
                del new[slot]
    # New-only slots can receive new IDs; old-only records are deletions.
    ambiguous = bool(old and new) or any(item["state"] == "unmatched" for item in previous)
    return bindings, list(old) if ambiguous else [], list(old) if not ambiguous else []
