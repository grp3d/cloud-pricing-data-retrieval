"""
Contract validation helper (analysis C3, constitution IV): validates every JSON document
the code actually wrote under `<provider>/manifests/` against the published schemas.
"""

import json
import os

from jsonschema import Draft202012Validator, FormatChecker

SCHEMA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "src", "pipeline", "schemas"
)


def load_schema(name: str) -> dict:
    with open(os.path.join(SCHEMA_DIR, name), encoding="utf-8") as fh:
        return json.load(fh)


def validator(name: str) -> Draft202012Validator:
    return Draft202012Validator(load_schema(name), format_checker=FormatChecker())


def validate_manifest_doc(doc: dict) -> None:
    validator("manifest.schema.json").validate(doc)


def validate_latest_doc(doc: dict) -> None:
    validator("latest.schema.json").validate(doc)


def assert_valid_contract(store, provider: str = "aws") -> int:
    """Validate manifest.json, revisions/*.json and latest.json. Returns documents checked."""
    checked = 0
    for obj in store.list(f"{provider}/manifests/"):
        doc = json.loads(store.get_bytes(obj.key))
        if obj.key.endswith("/latest.json"):
            validate_latest_doc(doc)
        else:
            validate_manifest_doc(doc)
        checked += 1
    return checked
