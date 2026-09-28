import ast
import hashlib
import json
import pathlib
import unittest


class QuotaFixtureProvenanceTests(unittest.TestCase):
    def test_fixture_definitions_match_verified_upstream_hashes(self):
        directory = pathlib.Path(__file__).parent / "fixtures"
        provenance = json.loads((directory / "codex_upstream_provenance.json").read_text())
        source = (directory / provenance["fixture"]).read_text()
        actual = {
            node.name: hashlib.sha256((ast.get_source_segment(source, node) or "").encode()).hexdigest()
            for node in ast.parse(source).body
            if isinstance(node, (ast.FunctionDef, ast.ClassDef))
        }
        expected = {
            name: digest
            for entry in provenance["files"].values()
            for name, digest in entry["definitions"].items()
        }
        self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
