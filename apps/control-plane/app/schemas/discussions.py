"""Bounded preview protocol. Unsupported anchors/images are rejected, never guessed."""

import base64
import binascii
import json
import uuid
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator
from pydantic_core import PydanticSerializationError


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("version", "protocol_version", mode="before", check_fields=False)
    @classmethod
    def integer_version(cls, value):
        if type(value) is not int:
            raise ValueError("Protocol versions must be integers")
        return value


class TextNode(StrictModel):
    type: Literal["text"]
    text: str = Field(min_length=1, max_length=8192)
    marks: list[Literal["bold", "italic", "code", "strike"]] = Field(
        default_factory=list, max_length=4
    )

    @field_validator("marks")
    @classmethod
    def unique_marks(cls, marks):
        if len(marks) != len(set(marks)):
            raise ValueError("Duplicate text marks")
        return marks


class LinkNode(StrictModel):
    type: Literal["link"]
    url: str = Field(max_length=2048)
    children: list[TextNode] = Field(min_length=1, max_length=20)

    @field_validator("url")
    @classmethod
    def safe_url(cls, url):
        parsed = urlsplit(url)
        if (
            url != url.strip()
            or any(ord(c) < 32 for c in url)
            or parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("Links must use an absolute HTTP(S) URL without credentials")
        return url


class MentionNode(StrictModel):
    type: Literal["mention"]
    user_id: uuid.UUID


InlineNode = Annotated[TextNode | LinkNode | MentionNode, Field(discriminator="type")]


class Paragraph(StrictModel):
    type: Literal["paragraph"]
    children: list[InlineNode] = Field(min_length=1, max_length=100)


class CodeBlock(StrictModel):
    type: Literal["code"]
    text: str = Field(min_length=1, max_length=8192)
    language: str = Field(default="", max_length=32, pattern=r"^[a-zA-Z0-9_+.-]*$")


class ListBlock(StrictModel):
    type: Literal["list"]
    ordered: StrictBool = False
    items: list[Paragraph] = Field(min_length=1, max_length=100)


class MessageBody(StrictModel):
    version: Literal[1]
    blocks: list[Annotated[Paragraph | CodeBlock | ListBlock, Field(discriminator="type")]] = Field(
        min_length=1, max_length=100
    )

    @model_validator(mode="after")
    def bounded_body(self):
        try:
            encoded = self.model_dump_json().encode("utf-8")
        except (UnicodeError, PydanticSerializationError):
            raise ValueError("Message must contain valid Unicode") from None
        if len(encoded) > 32768:
            raise ValueError("Message body exceeds 32 KiB")

        def meaningful(node):
            if isinstance(node, dict):
                return (
                    node.get("type") == "mention"
                    or bool(str(node.get("text", "")).strip())
                    or any(meaningful(v) for v in node.values() if isinstance(v, (dict, list)))
                )
            return isinstance(node, list) and any(meaningful(v) for v in node)

        if not meaningful(self.model_dump(mode="json")):
            raise ValueError("Message cannot be blank")
        return self

    def mentioned_users(self) -> set[uuid.UUID]:
        found = set()

        def visit(node):
            if isinstance(node, dict):
                if node.get("type") == "mention":
                    found.add(uuid.UUID(node["user_id"]))
                for value in node.values():
                    visit(value)
            elif isinstance(node, list):
                for value in node:
                    visit(value)

        visit(self.model_dump(mode="json"))
        return found


class NoteAnchor(StrictModel):
    version: Literal[1]
    type: Literal["note"]


class AnchorContext(StrictModel):
    quote: str = Field(max_length=512)
    prefix: str = Field(max_length=64)
    suffix: str = Field(max_length=64)
    truncated: StrictBool


class TextAnchor(StrictModel):
    version: Literal[1]
    type: Literal["text"]
    document_id: uuid.UUID
    generation: uuid.UUID
    dependency: str = Field(max_length=87384)
    start: str = Field(max_length=344)
    end: str = Field(max_length=344)
    selected_length: int = Field(ge=0, le=4294967295, strict=True)
    context: AnchorContext

    @field_validator("dependency", "start", "end")
    @classmethod
    def bounded_encoding(cls, value, info):
        limit = 65536 if info.field_name == "dependency" else 256
        try:
            decoded = base64.b64decode(value, validate=True)
            if not decoded or len(decoded) > limit or base64.b64encode(decoded).decode() != value:
                raise ValueError("Invalid anchor encoding")
        except (ValueError, binascii.Error):
            raise ValueError("Invalid anchor encoding") from None
        return value

    @model_validator(mode="after")
    def valid_unicode(self):
        try:
            self.model_dump_json().encode("utf-8")
        except (UnicodeError, PydanticSerializationError):
            raise ValueError("Anchor context must contain valid Unicode") from None
        return self


Anchor = Annotated[NoteAnchor | TextAnchor, Field(discriminator="type")]


class ThreadCreate(StrictModel):
    type: Literal["thread.create"]
    thread_id: uuid.UUID
    message_id: uuid.UUID
    anchor: Anchor
    body: MessageBody


class ReplyCreate(StrictModel):
    type: Literal["reply.create"]
    thread_id: uuid.UUID
    message_id: uuid.UUID
    body: MessageBody
    reopen: StrictBool = False
    expected_state_revision: int | None = Field(default=None, ge=1, strict=True)

    @model_validator(mode="after")
    def reopen_precondition(self):
        if self.reopen != (self.expected_state_revision is not None):
            raise ValueError("Send and reopen requires a state revision; ordinary replies do not")
        return self


class MessageEdit(StrictModel):
    type: Literal["message.edit"]
    thread_id: uuid.UUID
    message_id: uuid.UUID
    expected_revision: int = Field(ge=1, strict=True)
    body: MessageBody


class MessageDelete(StrictModel):
    type: Literal["message.delete"]
    thread_id: uuid.UUID
    message_id: uuid.UUID
    expected_revision: int = Field(ge=1, strict=True)
    reason: str | None = Field(default=None, min_length=1, max_length=512)


REACTION_KEYS = (
    "thumbs_up",
    "thumbs_down",
    "heart",
    "laugh",
    "celebrate",
    "eyes",
    "question",
    "check",
)


class ReactionSet(StrictModel):
    type: Literal["reaction.set"]
    thread_id: uuid.UUID
    message_id: uuid.UUID
    emoji: Literal[
        "thumbs_up", "thumbs_down", "heart", "laugh", "celebrate", "eyes", "question", "check"
    ]
    present: StrictBool


class ThreadState(StrictModel):
    type: Literal["thread.state"]
    thread_id: uuid.UUID
    expected_state_revision: int = Field(ge=1, strict=True)
    state: Literal["open", "resolved"]


class ThreadRemove(StrictModel):
    type: Literal["thread.remove"]
    thread_id: uuid.UUID
    expected_state_revision: int = Field(ge=1, strict=True)
    reason: str = Field(min_length=1, max_length=512)


Command = Annotated[
    ThreadCreate
    | ReplyCreate
    | MessageEdit
    | MessageDelete
    | ReactionSet
    | ThreadState
    | ThreadRemove,
    Field(discriminator="type"),
]


class CommandEnvelope(StrictModel):
    protocol_version: Literal[1]
    operation_id: uuid.UUID
    share_id: uuid.UUID
    entry_id: uuid.UUID
    command: Command

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
