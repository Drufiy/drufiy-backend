from typing import Literal
from pydantic import BaseModel, Field, field_validator


class FileChange(BaseModel):
    path: str = Field(..., description="File path relative to repo root")
    new_content: str = Field(..., description="Complete new file content (not a diff)")
    explanation: str = Field(..., description="What changed and why")

    @field_validator("path")
    @classmethod
    def validate_path(cls, v):
        if v.startswith("/") or ".." in v:
            raise ValueError("Path must be relative and must not contain '..'")
        return v

    @field_validator("new_content")
    @classmethod
    def validate_content(cls, v):
        if len(v) > 200_000:
            raise ValueError("new_content exceeds 200KB — likely hallucinated")
        if len(v.strip()) == 0:
            raise ValueError("new_content is empty")
        return v


class Diagnosis(BaseModel):
    problem_summary: str = Field(..., min_length=10, max_length=500)
    root_cause: str = Field(..., min_length=20, max_length=2000)
    fix_description: str = Field(..., min_length=20, max_length=2000)
    fix_type: Literal["safe_auto_apply", "review_recommended", "manual_required"]
    confidence: float = Field(..., ge=0.0, le=1.0)
    is_flaky_test: bool = Field(default=False)
    files_changed: list[FileChange] = Field(default_factory=list)
    category: Literal["code", "workflow_config", "dependency", "environment", "flaky_test", "unknown"]
    logs_truncated_warning: bool = Field(default=False)

    @field_validator("files_changed")
    @classmethod
    def validate_files_for_fix_type(cls, v, info):
        fix_type = info.data.get("fix_type")
        if fix_type == "manual_required" and len(v) > 0:
            raise ValueError("manual_required diagnoses must have empty files_changed")
        # NOTE: review_recommended with no files is handled as a business-rule downgrade
        # to manual_required in diagnose_failure() — do NOT raise here, or the whole
        # diagnosis is rejected and the run fails instead of gracefully degrading.
        if fix_type == "safe_auto_apply" and len(v) == 0:
            raise ValueError("safe_auto_apply must have at least one file change")
        return v
