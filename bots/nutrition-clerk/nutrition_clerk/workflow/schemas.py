"""Structured shapes for the workflow.

Kept intentionally small for N1 (SHAPE A cache-hit only). Later milestones
add fields as new shapes come online:
- N2: kcal / protein_g / fat_g / carbs_g + save_to_cache (SHAPE B)
- N3: reference_recent (SHAPE 6.4)
- N4: clarification_question (M5)
- N5: photo_index (SHAPE C/D)
- N6: datetime_hint fully wired

Each field addition is a schema evolution — the extractor prompt lists which
fields to populate for which pattern.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ExtractedEntry(BaseModel):
    """One food entry parsed out of a user message."""

    name: str = Field(description="Canonical or free-text food name (title case).")
    qty: float | None = Field(
        default=None,
        description="Quantity in the entry's unit. Defaults to 1 in orchestrator when None.",
    )
    unit: str | None = Field(
        default=None,
        description='Unit: "serving", "g", "100g", "cup", "slice", etc. Defaults to "serving" when None.',
    )
    datetime_hint: str | None = Field(
        default=None,
        description=(
            'Free-text time hint: "this morning", "yesterday 8pm", "just now", '
            'or a pre-structured "DD-MM-YYYY HH:MM". None = orchestrator uses '
            "datetime.now(). Resolved deterministically in Python."
        ),
    )
    photo_index: int | None = Field(
        default=None,
        description=(
            "0-based position of this entry among the entries the user marked "
            'as having a photo ("(label attached)", "photo attached", "see '
            'photo"). First marked entry -> 0, second -> 1, and so on. None '
            "when the user did not mark this entry. The extractor is TEXT-ONLY "
            "and never sees the images, so this records marker ORDER, not "
            "image content — the orchestrator verifies it against what vision "
            "actually read."
        ),
    )
    # N2 — SHAPE B: user typed full macros. When kcal is not None, orchestrator
    # skips the cache lookup and logs with source="text_estimate".
    kcal: float | None = Field(
        default=None,
        description=(
            "Total calories the user stated (e.g. '300 kcal'). When set, "
            "the entry is treated as SHAPE B and the cache is skipped."
        ),
    )
    protein_g: float | None = Field(
        default=None, description="Total protein in grams (SHAPE B)."
    )
    fat_g: float | None = Field(
        default=None, description="Total fat in grams (SHAPE B)."
    )
    carbs_g: float | None = Field(
        default=None, description="Total carbs in grams (SHAPE B)."
    )
    save_to_cache: bool = Field(
        default=False,
        description=(
            'True when the user asks to save/remember the item ("save it", '
            '"remember this", "add to cache"). Orchestrator will call '
            "add_personal_food after logging. Silently ignored for pure "
            "cache-hit entries (already saved) and for future knowledge-"
            "estimated entries (untrusted for cache seeding)."
        ),
    )
    # N3 — SHAPE 6.4: "save the pomegranate juice I had" — the user references
    # a food they logged EARLIER (in this session) and asks to promote it to
    # personal cache. When set, `save_to_cache` should also be True, and the
    # orchestrator looks up the entry in ChatContext.recent_entries rather
    # than logging a new row.
    reference_recent: bool = Field(
        default=False,
        description=(
            'True when the user references a previously-logged meal to save '
            'or reuse it (e.g. "save the pomegranate juice I had", "save '
            'the salmon poke from earlier", "remember that soup"). '
            "Orchestrator looks up the entry in the recent-meals ring by "
            "fuzzy-matching `name`, then calls add_personal_food using the "
            "cached per-unit values. Does NOT log a new row."
        ),
    )


class KnowledgeExtract(BaseModel):
    """Macro estimate for a food that isn't in either cache (N6).

    The model is asked to refuse rather than guess when the food is branded,
    regional, or otherwise too variable to estimate within ~15%. Refused
    entries surface to the user as "send me macros or a label photo".
    """

    refused: bool = Field(
        default=False,
        description=(
            "True when the food is branded, a complex restaurant/homemade "
            "dish, or otherwise too variable to estimate reliably. When "
            "true, all macro fields are ignored."
        ),
    )
    refusal_reason: str | None = Field(
        default=None,
        description="Short reason shown to the user when refused=true.",
    )
    unit: str = Field(
        default="serving",
        description=(
            'Basis for the per-unit values: "100g" for weight-based foods '
            '(nuts, cheese, grains, meat), otherwise a natural unit '
            '("serving", "cup", "slice", "egg", "banana").'
        ),
    )
    kcal_per_unit: float = Field(default=0, description="Calories per unit.")
    protein_per_unit: float = Field(default=0, description="Protein grams per unit.")
    fat_per_unit: float = Field(default=0, description="Fat grams per unit.")
    carbs_per_unit: float = Field(default=0, description="Carb grams per unit.")
    confidence: float = Field(
        default=0.5,
        description=(
            "0.4-0.7. Use the upper end for standardised whole foods "
            "(banana, olive oil, chicken breast), the lower end when the "
            "portion or preparation is ambiguous."
        ),
    )
    note: str | None = Field(
        default=None,
        description="Optional caveat about the assumption made (portion, preparation).",
    )

    @field_validator(
        "kcal_per_unit", "protein_per_unit", "fat_per_unit",
        "carbs_per_unit", "confidence", "unit",
        mode="before",
    )
    @classmethod
    def _null_to_default(cls, v, info):
        """Models legitimately return null for macro fields when refusing —
        they have no values to give. Coerce those to the field default so
        validation succeeds; the orchestrator ignores them when refused=True.
        """
        if v is None:
            return cls.model_fields[info.field_name].default
        return v


class LabelExtract(BaseModel):
    """Per-100g nutrition values extracted by the vision enricher from a
    packaging label photo. Fields deliberately mirror what a UK/EU nutrition
    panel lists — one row per macro.

    Drinks are labelled PER 100ML, and the model names its fields after what it
    reads: a Lucky Saint lager label came back as `kcal_per_100ml: 16` with the
    note "this is a beverage label showing nutrition per 100ml, not per 100g".
    Pydantic ignored the unknown keys, every macro defaulted to 0, and a
    correctly-read label logged as zero calories. `model_validate` now folds
    per-100ml keys onto the per-100g fields — for a drink the two bases are
    equivalent to within the density of water, which is the assumption the
    label itself is making.
    """

    model_config = ConfigDict(populate_by_name=True)

    @model_validator(mode="before")
    @classmethod
    def _accept_per_100ml_keys(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        out = dict(data)
        for macro in ("kcal", "protein", "fat", "carbs"):
            ml, g = f"{macro}_per_100ml", f"{macro}_per_100g"
            # Only fill a field the model did not already give us per-100g.
            if ml in out and out.get(g) in (None, 0):
                out[g] = out.pop(ml)
            else:
                out.pop(ml, None)
        return out

    label_name: str | None = Field(
        default=None,
        description=(
            "Product name if visible on the label (brand + product, e.g. "
            '"Waitrose Manchego" or "Metcalfe rice cracker"). Null when '
            "no product name is legible in the crop."
        ),
    )
    kcal_per_100g: float = Field(default=0, description="Calories per 100g. Use kcal not kJ.")
    protein_per_100g: float = Field(default=0, description="Protein grams per 100g.")
    fat_per_100g: float = Field(default=0, description="Fat grams per 100g (total, not saturates).")
    carbs_per_100g: float = Field(default=0, description="Carbohydrate grams per 100g (total, not sugars).")
    confidence_note: str | None = Field(
        default=None,
        description=(
            "Free-text note if any field was hard to read (glare, blur, partial "
            "crop, '<0.5g' encoded as 0.5, per-serving instead of per-100g, ...). "
            "Null when everything was clean."
        ),
    )

    @field_validator(
        "kcal_per_100g", "protein_per_100g", "fat_per_100g", "carbs_per_100g",
        mode="before",
    )
    @classmethod
    def _null_to_zero(cls, v):
        """A model handed a NON-label photo legitimately returns nulls here.
        Coercing to 0 keeps validation from throwing — the caller checks
        `PhotoExtract.kind` before trusting these values. Without this, a
        meal photo raised ValidationError, the turn failed, the Telegram
        offset was never committed, and the message redelivered forever.
        """
        return 0 if v is None else v


class MealDish(BaseModel):
    """One dish estimated from a photo of a plated meal (SHAPE D)."""

    name: str = Field(description="Dish name, matching what the user called it.")
    qty: float = Field(default=1, description="Number of units/servings consumed.")
    unit: str = Field(
        default="serving",
        description='Natural unit: "serving", "bowl", "slice", "piece", "cup".',
    )
    kcal_per_unit: float = Field(default=0)
    protein_per_unit: float = Field(default=0)
    fat_per_unit: float = Field(default=0)
    carbs_per_unit: float = Field(default=0)
    confidence: float = Field(
        default=0.4,
        description="0.3-0.5. Portion is estimated visually, so stay in this band.",
    )
    note: str | None = Field(
        default=None, description="Portion assumption, e.g. '~350g bowl'."
    )

    @field_validator(
        "qty", "kcal_per_unit", "protein_per_unit", "fat_per_unit",
        "carbs_per_unit", "confidence", "unit",
        mode="before",
    )
    @classmethod
    def _null_to_default(cls, v, info):
        if v is None:
            return cls.model_fields[info.field_name].default
        return v


class PhotoExtract(BaseModel):
    """Discriminated result of a single vision call on an attached photo.

    One LLM call decides what the photo actually is, so we never try to read
    a nutrition panel off a plate of curry (or vice-versa).
    """

    kind: str = Field(
        default="unclear",
        description=(
            'One of: "label" (packaged product nutrition panel), '
            '"meal" (plated food / restaurant table / dish), '
            '"unclear" (neither is legible or it is not food).'
        ),
    )
    label: LabelExtract | None = Field(
        default=None, description='Populated only when kind="label".'
    )
    dishes: list[MealDish] = Field(
        default_factory=list, description='Populated only when kind="meal".'
    )
    unclear_reason: str | None = Field(
        default=None, description='Short explanation when kind="unclear".'
    )


class ExtractedMessage(BaseModel):
    """The extractor's structured output for one inbound user message."""

    is_food_related: bool = Field(
        default=True,
        description=(
            "False if the message is not about eating / logging food (e.g. a "
            "recipe question). When False, `entries` should be empty."
        ),
    )
    entries: list[ExtractedEntry] = Field(default_factory=list)
    clarification_question: str | None = Field(
        default=None,
        description=(
            "Set when the user's message is genuinely too vague to parse (e.g. "
            '"some chia" with no quantity, or a food name with no hint of how '
            "much they ate). When set, `entries` should be empty and the "
            "orchestrator short-circuits — the question is sent to the user, "
            "who answers in the next turn. Do NOT set for common typos or "
            "ambiguity that can be resolved by cache lookup — that's the "
            "orchestrator's job."
        ),
    )
