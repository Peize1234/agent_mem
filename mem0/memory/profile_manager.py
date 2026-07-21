from typing import Any, Dict, Optional

from mem0.configs.base import UserProfileConfig
from mem0.memory.profile_schema import ProfileUpdatePlan
from mem0.memory.profile_validator import serialize_profile_value, validate_operation


class ProfileManager:
    def __init__(self, db, config: Optional[UserProfileConfig] = None):
        self.db = db
        self.config = config or UserProfileConfig()

    def list_attributes(self):
        """Return all active profile attribute definitions."""
        return self.db.list_profile_attributes(active_only=True)

    def get_profile(self, user_id: str, include_metadata: Optional[bool] = None) -> Dict[str, Any]:
        """Return the user's current cross-session profile."""
        if not user_id:
            raise ValueError("user_id is required")
        if include_metadata is None:
            include_metadata = self.config.include_metadata_by_default

        profile = {}
        for row in self.db.get_user_profile_values(user_id):
            if include_metadata:
                profile[row["attribute_key"]] = {
                    "value": row["value"],
                    "attribute_name": row["attribute_name"],
                    "category": row["attribute_category"],
                    "description": row["description"],
                    "value_type": row["value_type"],
                    "source_type": row["source_type"],
                    "confidence": row["confidence"],
                    "value_version": row["value_version"],
                }
            else:
                profile[row["attribute_key"]] = row["value"]
        return {"user_id": user_id, "profile": profile}

    def validate_update_plan(self, plan: Any) -> ProfileUpdatePlan:
        """Validate operation limits, known attributes, and value schemas."""
        update_plan = plan if isinstance(plan, ProfileUpdatePlan) else ProfileUpdatePlan.model_validate(plan)
        if len(update_plan.operations) > self.config.max_operations_per_update:
            raise ValueError("profile update exceeds max_operations_per_update")

        definitions = {item["attribute_key"]: item for item in self.list_attributes()}
        for operation in update_plan.operations:
            definition = definitions.get(operation.attribute_key)
            if definition is None:
                raise ValueError(f"Unknown or inactive profile attribute: {operation.attribute_key}")
            validate_operation(definition, operation)
            if operation.operation == "set":
                value_json = serialize_profile_value(operation.value)
                if len(value_json.encode("utf-8")) > self.config.max_value_json_bytes:
                    raise ValueError(
                        f"Profile attribute '{operation.attribute_key}' exceeds max_value_json_bytes"
                    )
        return update_plan

    def apply_update_plan(self, user_id: str, plan: Any) -> Dict[str, Any]:
        """Validate and atomically apply a profile update plan."""
        if not user_id:
            raise ValueError("user_id is required")
        update_plan = self.validate_update_plan(plan)
        self.db.apply_profile_update_plan(
            user_id,
            update_plan,
            max_value_json_bytes=self.config.max_value_json_bytes,
        )
        return self.get_profile(user_id)

    def delete_value(self, user_id: str, attribute_key: str) -> bool:
        """Delete one current user value while preserving its definition."""
        if not user_id:
            raise ValueError("user_id is required")
        return self.db.delete_user_profile_value(user_id, attribute_key)

    def delete_profile(self, user_id: str) -> int:
        """Delete all current profile values for one user."""
        if not user_id:
            raise ValueError("user_id is required")
        return self.db.delete_all_user_profile_values(user_id)

    def create_attribute(self, definition: Any) -> Dict[str, Any]:
        """Create a manually managed profile attribute definition."""
        if self.config.allow_dynamic_attributes is not True:
            raise ValueError(
                "Dynamic profile attributes are disabled; set profile.allow_dynamic_attributes=True before creating one"
            )
        dynamic_count = sum(not item["is_predefined"] for item in self.db.list_profile_attributes(active_only=False))
        if dynamic_count >= self.config.max_dynamic_attributes:
            raise ValueError(
                f"Dynamic profile attribute limit reached (max_dynamic_attributes={self.config.max_dynamic_attributes})"
            )
        return self.db.create_profile_attribute(definition, is_predefined=False)
