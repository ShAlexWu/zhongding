"""Streaming index tables (architecture doc section 8 pit 1).

The 23MB api.json referencedComponents (16,712 items) must never be fully loaded.
Indexing extracts only the fields rules need, via ijson, into these tables.
The 4 core business tables stay as locked in SPEC section 6; these are
infrastructure index tables consumed exclusively by the rule engine.
"""

from __future__ import annotations

from sqlalchemy import JSON, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ApiProp(Base):
    """customProperties of an api.json file (small: ~64 rows per drawing)."""

    __tablename__ = "api_props"
    __table_args__ = (
        UniqueConstraint("file_id", "prop_name", name="uq_file_prop"),
        Index("ix_api_props_name", "prop_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    file_id: Mapped[str] = mapped_column(ForeignKey("project_files.id", ondelete="CASCADE"))
    project_id: Mapped[str] = mapped_column(String(32), index=True)
    code: Mapped[str] = mapped_column(String(32), index=True)
    prop_name: Mapped[str] = mapped_column(String(64))
    raw_value: Mapped[str] = mapped_column(Text, default="")
    resolved_value: Mapped[str] = mapped_column(Text, default="")


class SpatialComponent(Base):
    """Flattened components of a spatial.json file, fields rules need."""

    __tablename__ = "spatial_components"
    __table_args__ = (Index("ix_spatial_code_name", "code", "name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    file_id: Mapped[str] = mapped_column(ForeignKey("project_files.id", ondelete="CASCADE"))
    project_id: Mapped[str] = mapped_column(String(32), index=True)
    code: Mapped[str] = mapped_column(String(32), index=True)
    comp_id: Mapped[str] = mapped_column(Text, default="")
    name: Mapped[str] = mapped_column(Text, default="")
    parent_id: Mapped[str] = mapped_column(Text, default="")
    path: Mapped[str] = mapped_column(Text, default="")
    depth: Mapped[int] = mapped_column(default=0)
    leaf: Mapped[bool] = mapped_column(default=False)
    material_name: Mapped[str] = mapped_column(Text, default="")
    thicknesses: Mapped[list] = mapped_column(JSON, default=list)
    transform_to_root: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    bounds_mm: Mapped[dict | None] = mapped_column(JSON, nullable=True)
