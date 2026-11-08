from typing import Literal

from pydantic import BaseModel, Field


class NavigateRequest(BaseModel):
    url: str
    wait_until: Literal["load", "domcontentloaded", "networkidle", "commit"] = "load"


class ClickRequest(BaseModel):
    selector: str = Field(
        description="Playwright selector: CSS, text=..., or role=button[name='...']"
    )
    timeout: int = Field(10_000, ge=100, le=120_000)


class TypeRequest(BaseModel):
    selector: str
    text: str = Field(max_length=20_000)
    submit: bool = Field(False, description="Press Enter after typing")
    clear: bool = Field(True, description="Replace the field's value instead of appending")
    timeout: int = Field(10_000, ge=100, le=120_000)


class PressRequest(BaseModel):
    key: str = Field(description="Key name such as Enter, Tab, Escape, Control+A")
    selector: str | None = None


class ClickAtRequest(BaseModel):
    x: float = Field(ge=0, description="Viewport X in CSS pixels, as on a screenshot")
    y: float = Field(ge=0, description="Viewport Y in CSS pixels, as on a screenshot")
    humanize: bool = Field(False, description="Move cursor along a human Bezier trajectory")


class KeyboardTypeRequest(BaseModel):
    text: str = Field(max_length=20_000)
    humanize: bool = Field(False, description="Type with natural per-character cadence")


class ScrollRequest(BaseModel):
    delta_y: float = Field(380.0, ge=-10_000, le=10_000)
    delta_x: float = Field(0.0, ge=-10_000, le=10_000)
    steps: int = Field(6, ge=1, le=50)
    humanize: bool = Field(True, description="Smooth multi-step scroll")


class WaitRequest(BaseModel):
    selector: str
    timeout: int = Field(15_000, ge=100, le=120_000)


class EvaluateRequest(BaseModel):
    script: str = Field(
        max_length=200_000,
        description="JavaScript expression or function body, e.g. document.title",
    )


class TabNewRequest(BaseModel):
    url: str | None = None
