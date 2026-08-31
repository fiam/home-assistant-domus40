"""Translation catalog contracts."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

COMPONENT = Path(__file__).parents[2] / "custom_components" / "domus40"


def _leaf_paths(value: Any, prefix: tuple[str, ...] = ()) -> set[tuple[str, ...]]:
    """Return every translated leaf path in a JSON catalog."""
    if not isinstance(value, dict):
        return {prefix}
    return {
        path
        for key, child in value.items()
        for path in _leaf_paths(child, (*prefix, key))
    }


class TranslationCatalogTests(unittest.TestCase):
    """Keep every shipped language structurally complete."""

    def test_english_portuguese_and_spanish_have_identical_keys(self) -> None:
        translations = COMPONENT / "translations"
        catalogs = {
            language: json.loads((translations / f"{language}.json").read_text())
            for language in ("en", "pt", "es")
        }
        expected = _leaf_paths(catalogs["en"])

        self.assertEqual(_leaf_paths(catalogs["pt"]), expected)
        self.assertEqual(_leaf_paths(catalogs["es"]), expected)

        strings = json.loads((COMPONENT / "strings.json").read_text())
        strings.pop("title")
        self.assertEqual(_leaf_paths(strings), expected)


if __name__ == "__main__":
    unittest.main()
