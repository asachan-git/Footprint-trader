"""Decision schema — pydantic model + matching Claude tool definition.

Claude is forced to call the `submit_decision` tool, returning structured data
that maps 1:1 to `Decision`. No free-text parsing.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Side = Literal["long", "short", "flat"]


class Decision(BaseModel):
    side: Side = Field(description="long | short | flat (no trade)")
    entry: float | None = Field(None, description="Entry price; required if side != flat")
    stop_loss: float | None = Field(None, description="Stop loss; required if side != flat")
    take_profit: float | None = Field(None, description="Take profit; required if side != flat")
    confidence: float = Field(ge=0.0, le=1.0, description="0..1 model confidence")
    rationale: str = Field(default="", description="One- or two-sentence reason citing footprint features")
    # Grid fields (Phase 2)
    grid_leg: int = Field(default=1, description="Which leg of the grid (1=first entry, 2=add, 3=add)")
    parent_position_id: str | None = Field(None, description="Position ID to add a leg to; None = new grid")
    add_to_existing: bool = Field(default=False, description="True = add leg to active grid; False = new position")
    invalidation_note: str = Field(default="", description="What footprint event would invalidate this trade")


CLAUDE_TOOL = {
    "name": "submit_decision",
    "description": "Submit a trading decision based on the footprint and VP analysis.",
    "input_schema": {
        "type": "object",
        "properties": {
            "side": {"type": "string", "enum": ["long", "short", "flat"]},
            "entry": {"type": ["number", "null"]},
            "stop_loss": {"type": ["number", "null"]},
            "take_profit": {"type": ["number", "null"]},
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "rationale": {"type": "string"},
            "grid_leg": {"type": "integer", "minimum": 1, "maximum": 3},
            "parent_position_id": {"type": ["string", "null"]},
            "add_to_existing": {"type": "boolean"},
            "invalidation_note": {"type": "string"},
        },
        "required": ["side", "confidence", "rationale"],
    },
}
