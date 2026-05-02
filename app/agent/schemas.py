from typing import Literal
from pydantic import BaseModel, Field, field_validator, model_validator


class FileChange(BaseModel):
    path: str = Field(..., description="File path relative to repo root")
    new_content: str | None = Field(default=None, description="Complete new file content")
    patch: str | None = Field(default=None, description="Unified diff patch to apply to the current file")
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
        if v is None:
            return v
        if len(v) > 200_000:
            raise ValueError("new_content exceeds 200KB — likely hallucinated")
        if len(v.strip()) == 0:
            raise ValueError("new_content is empty")
        return v

    @field_validator("patch")
    @classmethod
    def validate_patch(cls, v):
        if v is None:
            return v
        if len(v) > 200_000:
            raise ValueError("patch exceeds 200KB — likely hallucinated")
        if len(v.strip()) == 0:
            raise ValueError("patch is empty")
        return v

    @model_validator(mode="after")
    def require_content_or_patch(self) -> "FileChange":
        if not self.new_content and not self.patch:
            raise ValueError("Either new_content or patch must be provided")
        return self


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
    speculative: bool = Field(default=False, description="True when confidence is low but a best-guess PR is still created for review")

    @model_validator(mode="after")
    def coerce_fix_type(self) -> "Diagnosis":
        """
        Auto-coerce inconsistent fix_type / files_changed combinations instead
        of hard-failing validation and killing the entire pipeline run.

        Rules:
        - safe_auto_apply or review_recommended with NO files → downgrade to manual_required
          (model said it would fix but produced nothing — treat as "can't fix")
        - manual_required WITH files → upgrade to review_recommended
          (model said it couldn't fix but produced a fix anyway — surface it for review)
        """
        has_files = bool(self.files_changed)
        if self.fix_type in ("safe_auto_apply", "review_recommended") and not has_files:
            self.fix_type = "manual_required"
        elif self.fix_type == "manual_required" and has_files:
            self.fix_type = "review_recommended"
        return self
