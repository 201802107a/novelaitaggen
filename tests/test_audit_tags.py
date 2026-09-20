import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from audit_tags import audit, read_reference, refresh


def row(*tags):
    return {"category": "머리카락", "subCategory": "스타일", "name": "기존 이름",
            "tags": list(tags), "nsfw": False}


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.reference = {"long_hair": {"category": 0, "post_count": 100},
                          "smile": {"category": 0, "post_count": 80}}
        self.aliases = {"longhair": {"long_hair"}}

    def test_only_confirmed_spelling_changes(self):
        catalog = {"외모": [row("Long Hair", "longhair", "Best Quality", "detailed_*")]}
        original = copy.deepcopy(catalog)
        result, changes = refresh(catalog, self.reference, self.aliases, [])
        self.assertEqual(result["외모"][0]["tags"],
                         ["long_hair", "longhair", "Best Quality", "detailed_*"])
        self.assertEqual(catalog, original)
        self.assertEqual(len(changes), 1)

    def test_same_tag_in_different_contexts_is_preserved(self):
        catalog = {"외모": [row("Long Hair", "long_hair"), row("long_hair")]}
        result, changes = refresh(catalog, self.reference, self.aliases, [])
        self.assertEqual(len(result["외모"]), 2)
        self.assertEqual(result["외모"][0]["tags"], ["long_hair"])
        self.assertTrue(any(c["type"] == "duplicate_in_row" for c in changes))

    def test_audit_distinguishes_absent_alias_placeholder_and_match(self):
        data = {"외모": [row("Long Hair", "longhair", "best_quality", "detailed_*")]}
        statuses = {r["tag"]: r["status"] for r in audit(data, self.reference, self.aliases)}
        self.assertEqual(statuses, {"long_hair": "snapshot_match",
                                   "longhair": "alias_candidate_unverified",
                                   "best_quality": "not_in_snapshot",
                                   "detailed_*": "placeholder_review"})

    def test_exact_name_takes_priority_over_historical_alias(self):
        rows = audit({"외모": [row("smile")]}, self.reference, {"smile": {"long_hair"}})
        self.assertEqual(rows[0]["status"], "snapshot_match")

    def test_reviewed_additions_are_idempotent_and_keep_korean_metadata(self):
        additions = [{"group": "외모", "row": row("smile")}]
        first, changes = refresh({"외모": [row("Long Hair")]}, self.reference, self.aliases, additions)
        second, repeated = refresh(first, self.reference, self.aliases, additions)
        self.assertEqual(first, second)
        self.assertEqual(repeated, [])
        self.assertEqual(first["외모"][0]["name"], "기존 이름")

    def test_no_duplicate_addition_when_alias_already_represents_it(self):
        additions = [{"group": "외모", "row": row("long_hair")}]
        result, changes = refresh({"외모": [row("longhair")]}, self.reference, self.aliases, additions)
        self.assertEqual(len(result["외모"]), 1)
        self.assertEqual(changes, [])

    def test_reference_translation_is_not_an_alias(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "reference.csv"
            path.write_text('long_hair,0,100,"longhair,old_name",긴머리\n', encoding="utf-8")
            reference, aliases = read_reference(path)
            self.assertIn("long_hair", reference)
            self.assertEqual(set(aliases), {"longhair", "old_name"})

    def test_failed_validation_never_changes_catalog(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            catalog = root / "tags.json"
            catalog.write_text(json.dumps({"외모": [row("Long Hair")]}), encoding="utf-8")
            before = catalog.read_bytes()
            reference = root / "reference.csv"
            reference.write_text("long_hair,0,100,\n", encoding="utf-8")
            additions = root / "additions.json"
            additions.write_text(json.dumps([{"group": "외모", "row": row("unknown")}]))
            script = Path(__file__).resolve().parents[1] / "scripts/audit_tags.py"
            result = subprocess.run([sys.executable, str(script), "--catalog", str(catalog),
                "--reference", str(reference), "--additions", str(additions),
                "--source-url", "https://example.com/snapshot", "--source-date", "2026-09-12",
                "--output", str(root / "reports"), "--apply"], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(catalog.read_bytes(), before)
            self.assertFalse((root / "reports").exists())


if __name__ == "__main__":
    unittest.main()
