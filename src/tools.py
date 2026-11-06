"""Catalog tools exposed to the sales agent.

Two tools, deliberately split so every spec the agent states must come
from an explicit lookup we can log and check:
  - list_products(category) -> ids + names only
  - get_product(product_id) -> full record
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class Catalog:
    def __init__(self, path: str | Path):
        products = json.loads(Path(path).read_text(encoding="utf-8"))
        self.by_id: dict[str, dict] = {p["id"]: p for p in products}
        if len(self.by_id) != len(products):
            raise ValueError("Duplicate product ids in catalog")
        self.categories: list[str] = sorted({p["category"] for p in products})

    def in_category(self, category: str) -> list[dict]:
        return [p for p in self.by_id.values() if p["category"] == category]

    # ---- tool implementations -------------------------------------------
    def list_products(self, category: str) -> dict[str, Any]:
        if category not in self.categories:
            return {"error": f"Unknown category '{category}'.",
                    "valid_categories": self.categories}
        return {"category": category,
                "products": [{"id": p["id"], "name": p["name"]}
                             for p in self.in_category(category)]}

    def get_product(self, product_id: str) -> dict[str, Any]:
        p = self.by_id.get(product_id)
        if p is None:
            return {"error": f"No product with id '{product_id}'."}
        return p

    # ---- dispatch -------------------------------------------------------
    def execute(self, name: str, tool_input: dict) -> dict[str, Any]:
        if name == "list_products":
            return self.list_products(tool_input.get("category", ""))
        if name == "get_product":
            return self.get_product(tool_input.get("product_id", ""))
        return {"error": f"Unknown tool '{name}'."}

    def tool_schemas(self) -> list[dict]:
        """Anthropic tool definitions. Categories are listed so the agent
        never has to guess them."""
        return [
            {
                "name": "list_products",
                "description": ("List product ids and names in a category. "
                                f"Valid categories: {', '.join(self.categories)}. "
                                "Returns no specs; use get_product for details."),
                "input_schema": {
                    "type": "object",
                    "properties": {"category": {"type": "string",
                                                "enum": self.categories}},
                    "required": ["category"],
                },
            },
            {
                "name": "get_product",
                "description": ("Get the full specification, price and lead "
                                "time for one product by id."),
                "input_schema": {
                    "type": "object",
                    "properties": {"product_id": {"type": "string"}},
                    "required": ["product_id"],
                },
            },
        ]
