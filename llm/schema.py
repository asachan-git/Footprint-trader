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
    rationale: str = Field(default="", description="Full prose summary of the trade thesis")
    # Structured reasoning breakdown (non-flat decisions)
    entry_reasoning: str = Field(default="", description="2-3 bullet points (- prefix) citing specific footprint/VP levels that confirm the entry")
    sl_reasoning: str = Field(default="", description="1-2 bullets: structural level protecting SL + risk distance check")
    target_reasoning: str = Field(default="", description="1-2 bullets: specific TP level (naked POC, Fibonacci T1/T2, HVN, etc.)")
    # Grid fields (Phase 2)
    grid_leg: int = Field(default=1, description="Which leg of the grid (1=first entry, 2=add, 3=add)")
    parent_position_id: str | None = Field(None, description="Position ID to add a leg to; None = new grid")
    add_to_existing: bool = Field(default=False, description="True = add leg to active grid; False = new position")
    qty_pct: float = Field(default=1.0, ge=0.3, le=1.0, description="Fraction of risk-based size for this leg (0.3–1.0), from confluence strength. 1.0 = full size on strong confluence; 0.3 = weak.")
    bias_strength: int = Field(default=3, ge=1, le=5, description="Direction conviction 1-5. Drives total grid exposure: lots = ladder × BASE × (bias_strength/5). 5 = max conviction, 1 = weak.")
    invalidation_note: str = Field(default="", description="What footprint event would invalidate this trade")
    # Options fields — populated only for options instruments (dhan mode)
    option_type: Literal["CE", "PE", "NONE"] = Field(default="NONE", description="CE (buy call) | PE (buy put) | NONE (non-options)")
    option_strike: float | None = Field(None, description="Selected option strike price")
    option_expiry: str | None = Field(None, description="Option expiry date YYYY-MM-DD")
    option_security_id: str | None = Field(None, description="Dhan security_id for the selected option (from strike_candidates)")
    option_product: Literal["INTRA", "MARGIN", "NONE"] = Field(default="NONE", description="INTRA=MIS intraday | MARGIN=NRML carry | NONE=non-options")


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
            "rationale": {"type": "string", "description": "Full prose summary of the trade thesis"},
            "entry_reasoning": {
                "type": "string",
                "description": "2-3 bullet points (use '- ' prefix) citing specific footprint/VP levels confirming the entry. Example: '- Stacked buy imbalance 76710-76780 (3 levels)\\n- CVD_5bar +86 confirms buying\\n- Price reclaimed VAL 76540'"
            },
            "sl_reasoning": {
                "type": "string",
                "description": "1-2 bullets explaining the SL structural level + risk check. Example: '- Below stacked buy zone base at 76740 (invalidation)\\n- 80pts = 0.10% of entry, above 0.05% minimum'"
            },
            "target_reasoning": {
                "type": "string",
                "description": "1-2 bullets naming the specific TP level and why. Example: '- Prior day POC 76910 + weekly POC 76900 cluster\\n- T1 measured move at 76980'"
            },
            "grid_leg": {"type": "integer", "minimum": 1, "maximum": 3},
            "parent_position_id": {"type": ["string", "null"]},
            "add_to_existing": {"type": "boolean"},
            "qty_pct": {"type": "number", "minimum": 0.3, "maximum": 1.0,
                        "description": "Fraction of risk-based size (0.3–1.0) from confluence strength. Strong confluence (absorption + imbalance + HVN + CVD aligned) → 1.0; weak → 0.3."},
            "bias_strength": {"type": "integer", "minimum": 1, "maximum": 5,
                              "description": "Direction conviction 1-5. Scales grid total exposure. 5=max conviction (multi-TF aligned, clean structure), 3=balanced, 1=weak (mixed signals). Used by mechanical grid placer."},
            "invalidation_note": {"type": "string"},
            # Options fields — only required for options instruments
            "option_type": {
                "type": "string",
                "enum": ["CE", "PE", "NONE"],
                "description": "CE = buy call (long bias), PE = buy put (short bias), NONE = not an options trade",
            },
            "option_strike": {"type": ["number", "null"], "description": "Strike price selected from strike_candidates"},
            "option_expiry": {"type": ["string", "null"], "description": "Expiry date YYYY-MM-DD from strike_candidates"},
            "option_security_id": {
                "type": ["string", "null"],
                "description": "security_id from the selected candidate in strike_candidates — copy exactly",
            },
            "option_product": {
                "type": "string",
                "enum": ["INTRA", "MARGIN", "NONE"],
                "description": "INTRA for intraday (MIS, squared off 3:20 PM IST), MARGIN for overnight/swing (NRML)",
            },
        },
        "required": ["side", "confidence", "rationale"],
    },
}


# ── Grid Plan Schema (v4 — used when claude.mode = full or restricted) ─────────

class GridLegOrder(BaseModel):
    leg_idx: int = Field(ge=1, le=5)
    price: float
    qty: float = Field(gt=0)
    rationale: str = Field(default="", max_length=60)


class PositionManagement(BaseModel):
    tp_primary: float
    tp_extended: float | None = None
    sl: float
    max_legs: int = Field(ge=1, le=5, default=3)
    trail_after_r: float = Field(ge=0.0, default=2.0)
    be_after_r: float = Field(ge=0.0, default=1.0)
    tp_shrink_on_opposite_absorption: bool = True


class GridDecision(BaseModel):
    """Full grid plan returned by Claude in FULL or RESTRICTED mode."""
    side: Side
    confidence: float = Field(ge=0.0, le=1.0)
    grid_orders: list[GridLegOrder] = Field(default_factory=list)
    position_management: PositionManagement | None = None
    invalidation_note: str = Field(default="", max_length=160)
    rationale: str = Field(default="", max_length=300)


GRID_PLAN_TOOL = {
    "name": "submit_grid_plan",
    "description": "Submit a full grid execution plan with leg prices, quantities, and position management.",
    "input_schema": {
        "type": "object",
        "properties": {
            "side": {"type": "string", "enum": ["long", "short", "flat"]},
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "grid_orders": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "leg_idx":   {"type": "integer", "minimum": 1, "maximum": 5},
                        "price":     {"type": "number"},
                        "qty":       {"type": "number", "minimum": 0.001},
                        "rationale": {"type": "string", "maxLength": 60},
                    },
                    "required": ["leg_idx", "price", "qty"],
                },
                "description": "Empty array when side=flat.",
            },
            "position_management": {
                "type": "object",
                "properties": {
                    "tp_primary":   {"type": "number"},
                    "tp_extended":  {"type": ["number", "null"]},
                    "sl":           {"type": "number"},
                    "max_legs":     {"type": "integer", "minimum": 1, "maximum": 5},
                    "trail_after_r":{"type": "number", "minimum": 0},
                    "be_after_r":   {"type": "number", "minimum": 0},
                    "tp_shrink_on_opposite_absorption": {"type": "boolean"},
                },
                "required": ["tp_primary", "sl", "max_legs"],
            },
            "invalidation_note": {"type": "string", "maxLength": 160},
            "rationale": {"type": "string", "maxLength": 300},
        },
        "required": ["side", "confidence", "grid_orders"],
    },
}
