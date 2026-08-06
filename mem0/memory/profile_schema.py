from typing import Annotated, Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


class ProfileAttributeDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attribute_id: Optional[int] = None
    attribute_key: str = Field(
        min_length=2,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_]{1,63}$",
    )
    attribute_name: str
    attribute_category: str
    description: str
    value_type: Literal[
        "string",
        "number",
        "boolean",
        "string_list",
        "number_list",
        "object",
        "object_list",
    ]
    value_schema: Dict[str, Any]
    merge_policy: Literal["replace", "append_unique"] = "replace"
    is_predefined: bool = False
    is_active: bool = True
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class FinancialUserProfile(BaseModel):
    user_id: str
    profile: Dict[str, Any] = Field(default_factory=dict)


class ProfileOperationMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_type: Literal["explicit", "inferred", "repeated", "correction"]
    confidence: float = Field(ge=0, le=1)


class SetValueOperation(ProfileOperationMetadata):
    operation: Literal["set"]
    attribute_key: str
    value: Any


class AppendItemsOperation(ProfileOperationMetadata):
    operation: Literal["append_unique"]
    attribute_key: str
    items: List[Any]


class RemoveItemsOperation(ProfileOperationMetadata):
    operation: Literal["remove_items"]
    attribute_key: str
    items: List[Any]


class PatchObjectOperation(ProfileOperationMetadata):
    operation: Literal["patch_object"]
    attribute_key: str
    updates: Dict[str, Any] = Field(default_factory=dict)
    remove_keys: List[str] = Field(default_factory=list)


class DeleteValueOperation(ProfileOperationMetadata):
    operation: Literal["delete"]
    attribute_key: str


ProfileOperation = Annotated[
    Union[
        SetValueOperation,
        AppendItemsOperation,
        RemoveItemsOperation,
        PatchObjectOperation,
        DeleteValueOperation,
    ],
    Field(discriminator="operation"),
]


class UnmappedProfileFact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suggested_key: str = Field(
        min_length=2,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_]{1,63}$",
    )
    description: str
    value: Any


class ProfileUpdatePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operations: List[ProfileOperation] = Field(default_factory=list)
    unmapped_facts: List[UnmappedProfileFact] = Field(default_factory=list)
