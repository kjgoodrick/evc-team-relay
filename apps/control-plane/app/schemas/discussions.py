"""Bounded preview protocol. Unsupported anchors/images are rejected, never guessed."""

import base64
import binascii
import json
import uuid
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    field_validator,
    model_serializer,
    model_validator,
)
from pydantic_core import PydanticSerializationError

from app.schemas.base_values import GroupValue
from app.schemas.timeline_markers import TimelineMarkerContent
from app.schemas.timeline_ranges import TimelineRangeValue, zone


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


class PictureBlock(StrictModel):
    type: Literal["picture"]
    picture_id: uuid.UUID
    alt: str = Field(default="", max_length=1024)


class MessageBody(StrictModel):
    version: Literal[1]
    blocks: list[
        Annotated[Paragraph | CodeBlock | ListBlock | PictureBlock, Field(discriminator="type")]
    ] = Field(min_length=1, max_length=100)

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
                    node.get("type") in {"mention", "picture"}
                    or bool(str(node.get("text", "")).strip())
                    or any(meaningful(v) for v in node.values() if isinstance(v, (dict, list)))
                )
            return isinstance(node, list) and any(meaningful(v) for v in node)

        if not meaningful(self.model_dump(mode="json")):
            raise ValueError("Message cannot be blank")
        if len([block for block in self.blocks if isinstance(block, PictureBlock)]) > 10:
            raise ValueError("Message cannot contain more than ten pictures")
        return self

    def picture_ids(self) -> set[uuid.UUID]:
        return {block.picture_id for block in self.blocks if isinstance(block, PictureBlock)}

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


class BlockDescriptor(StrictModel):
    kind: Literal[
        "paragraph",
        "heading",
        "list_item",
        "quote",
        "callout",
        "code",
        "table",
        "html",
        "rule",
        "frontmatter",
    ]
    explicit_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9-]{1,64}$")


class BlockStructure(StrictModel):
    kind: Literal["blocks"]
    blocks: list[BlockDescriptor] = Field(min_length=1, max_length=32)


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
    structure: BlockStructure | None = None

    @model_serializer(mode="wrap")
    def preserve_legacy_serialization(self, handler):
        value = handler(self)
        # Existing command hashes/receipts must remain byte-for-byte stable
        # when the new optional block evidence is absent.
        if self.structure is None:
            value.pop("structure", None)
        else:
            for block in value["structure"]["blocks"]:
                if block.get("explicit_id") is None:
                    block.pop("explicit_id", None)
        return value

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


class ManagedImage(StrictModel):
    kind: Literal["managed"]
    entry_id: uuid.UUID
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ExternalImage(StrictModel):
    kind: Literal["external"]
    url: str = Field(max_length=2048)
    version_evidence: Literal["unknown"]

    @field_validator("url")
    @classmethod
    def safe_url(cls, value):
        if (
            not value.lower().startswith(("https://", "http://"))
            or "\\" in value
            or any(0xD800 <= ord(c) <= 0xDFFF for c in value)
        ):
            raise ValueError("Images require an unambiguous HTTP(S) URL")
        urlsplit(value).port  # Invalid brackets/ports must reject before storage.
        return LinkNode.safe_url(value)


Coordinate = Annotated[StrictFloat, Field(ge=0, le=1)]


class WholeImage(StrictModel):
    type: Literal["whole"]


class ImagePin(StrictModel):
    type: Literal["pin"]
    x: Coordinate
    y: Coordinate


class ImageRegion(ImagePin):
    type: Literal["region"]
    width: Annotated[StrictFloat, Field(gt=0, le=1)]
    height: Annotated[StrictFloat, Field(gt=0, le=1)]

    @model_validator(mode="after")
    def inside_image(self):
        if self.x + self.width > 1 or self.y + self.height > 1:
            raise ValueError("Region must remain inside the image")
        return self


class ImageBasis(StrictModel):
    width: int = Field(gt=0, le=100000, strict=True)
    height: int = Field(gt=0, le=100000, strict=True)
    orientation: Literal["display"]


class ImageAnchor(StrictModel):
    version: Literal[1]
    type: Literal["image"]
    occurrence: TextAnchor
    asset: Annotated[ManagedImage | ExternalImage, Field(discriminator="kind")]
    basis: ImageBasis
    geometry: Annotated[WholeImage | ImagePin | ImageRegion, Field(discriminator="type")]

    @model_validator(mode="after")
    def image_occurrence(self):
        if self.occurrence.structure is not None or self.occurrence.selected_length == 0:
            raise ValueError("An image occurrence requires a nonempty embed range")
        return self


class ImageFileAnchor(StrictModel):
    version: Literal[1]
    type: Literal["image_file"]
    generation: uuid.UUID
    asset: ManagedImage
    basis: ImageBasis
    geometry: Annotated[WholeImage | ImagePin | ImageRegion, Field(discriminator="type")]


class BaseProperty(StrictModel):
    source: Literal["note", "file"]
    name: str = Field(min_length=1, max_length=256)

    @field_validator("name")
    @classmethod
    def valid_name(cls, value):
        if any(ord(c) < 32 for c in value):
            raise ValueError("Invalid property name")
        try:
            value.encode("utf-8")
        except UnicodeError:
            raise ValueError("Invalid property name") from None
        return value


class BaseViewTarget(StrictModel):
    type: Literal["view"]


class BaseItemTarget(StrictModel):
    type: Literal["item"]
    record_id: uuid.UUID
    record_generation: uuid.UUID


class BaseCellTarget(BaseItemTarget):
    type: Literal["cell"]
    property: BaseProperty


class BasePropertyTarget(StrictModel):
    type: Literal["property"]
    property: BaseProperty


class BaseDefinitionProperty(BaseProperty):
    source: Literal["note", "file", "formula"]


class BaseFormulaProperty(BaseProperty):
    source: Literal["formula"]


class BaseDefinedTarget(StrictModel):
    definition_id: uuid.UUID
    definition_revision: int = Field(ge=1, le=9007199254740991, strict=True)


class BaseFormulaTarget(BaseDefinedTarget):
    type: Literal["formula"]
    property: BaseFormulaProperty


class BaseSummaryTarget(BaseDefinedTarget):
    type: Literal["summary"]
    property: BaseDefinitionProperty
    summary: str = Field(min_length=1, max_length=256)

    @field_validator("summary")
    @classmethod
    def valid_summary(cls, value):
        return BaseProperty.valid_name(value)


class BaseFormulaCellTarget(BaseDefinedTarget, BaseItemTarget):
    type: Literal["formula_cell"]
    property: BaseFormulaProperty


class BaseGroupTarget(BaseDefinedTarget):
    type: Literal["group"]
    property: BaseDefinitionProperty
    group_id: uuid.UUID
    witness_id: uuid.UUID
    value: GroupValue


class TimelineAxis(StrictModel):
    version: Literal[1]
    renderer: Literal["sabidurian-timeline", "sabidurian-sequence"]
    start_property: BaseDefinitionProperty
    end_property: BaseDefinitionProperty | None
    time_zone: str | None = Field(max_length=128)

    @field_validator("time_zone")
    @classmethod
    def valid_zone(cls, value):
        return None if value is None else zone(value)


class BaseTimelineRangeTarget(StrictModel):
    type: Literal["timeline_range"]
    axis_definition_id: uuid.UUID
    axis_definition_revision: int = Field(ge=1, le=9007199254740991, strict=True)
    axis: TimelineAxis
    range: TimelineRangeValue
    lane: BaseGroupTarget | None = None

    @model_validator(mode="after")
    def coordinate_system(self):
        sequence = self.axis.renderer == "sabidurian-sequence"
        if (
            sequence != (self.range["kind"] == "ordinal")
            or sequence
            and self.axis.time_zone is not None
        ):
            raise ValueError("Timeline coordinate system does not match the saved axis")
        if (
            self.range["kind"] in {"calendar", "instant"}
            and self.axis.time_zone is not None
            and self.axis.time_zone != self.range["time_zone"]
        ):
            raise ValueError("Timeline timezone does not match the saved axis")
        return self


class BaseTimelineMarkerTarget(StrictModel):
    type: Literal["timeline_marker"]
    marker_id: uuid.UUID
    marker_revision: int = Field(ge=1, le=9007199254740991, strict=True)
    axis_definition_id: uuid.UUID
    axis_definition_revision: int = Field(ge=1, le=9007199254740991, strict=True)
    axis: TimelineAxis
    content: TimelineMarkerContent

    @model_validator(mode="after")
    def coordinate_system(self):
        BaseTimelineRangeTarget.model_validate(
            dict(
                type="timeline_range",
                axis_definition_id=self.axis_definition_id,
                axis_definition_revision=self.axis_definition_revision,
                axis=self.axis,
                range=self.content.range,
            )
        )
        return self


class BaseAnchor(StrictModel):
    version: Literal[1]
    type: Literal["base"]
    definition_id: uuid.UUID
    view_id: uuid.UUID
    generation: uuid.UUID
    registry_revision: int = Field(ge=1, le=9007199254740991, strict=True)
    target: Annotated[
        BaseViewTarget
        | BaseItemTarget
        | BaseCellTarget
        | BasePropertyTarget
        | BaseFormulaTarget
        | BaseSummaryTarget
        | BaseFormulaCellTarget
        | BaseGroupTarget
        | BaseTimelineRangeTarget
        | BaseTimelineMarkerTarget,
        Field(discriminator="type"),
    ]


Anchor = Annotated[
    NoteAnchor | TextAnchor | ImageAnchor | ImageFileAnchor | BaseAnchor,
    Field(discriminator="type"),
]


def source_anchor(anchor: Anchor) -> TextAnchor | None:
    return (
        anchor.occurrence
        if isinstance(anchor, ImageAnchor)
        else anchor
        if isinstance(anchor, TextAnchor)
        else None
    )


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
