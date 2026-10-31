"""
Code organization (constitution v1.3.0, Principle VI rules 1 and 4; feature 004 T006).

- Shared code under src/pipeline/ is provider-neutral: it neither imports a provider
  package nor names a provider. The registry src/pipeline/providers.py is the only
  exception.
- Provider packages (src/aws/, src/azure/, src/gcp/) never import each other.
- Provider code lives in its package: no top-level src/<provider>_*.py modules.

Docstrings and comments are ignored: only code (imports, names, string constants) counts.
"""

import ast
import os
import re
from typing import Iterator, List, Tuple

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
SRC = os.path.join(REPO, "src")
PIPELINE = os.path.join(SRC, "pipeline")
PROVIDERS = ("aws", "azure", "gcp")
REGISTRY = os.path.join(PIPELINE, "providers.py")


def _py_files(root: str) -> Iterator[str]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in sorted(filenames):
            if name.endswith(".py"):
                yield os.path.join(dirpath, name)


def _rel(path: str) -> str:
    return os.path.relpath(path, REPO)


def _tokens(text: str) -> List[str]:
    """Split on non-alphanumerics and camelCase boundaries: AwsPricingDownloader → aws, pricing, downloader."""
    words = []
    for chunk in re.split(r"[^A-Za-z0-9]+", text):
        words += re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+", chunk)
    return [w.lower() for w in words]


def _docstring_nodes(tree: ast.AST) -> set:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    ids.add(id(body[0].value))
    return ids


def _imported_modules(tree: ast.AST) -> List[Tuple[int, str]]:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(node.lineno, alias.name) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append((node.lineno, node.module))
            found += [(node.lineno, f"{node.module}.{alias.name}") for alias in node.names]
    return found


def _provider_of(module: str):
    parts = module.split(".")
    if len(parts) >= 2 and parts[0] == "src" and parts[1] in PROVIDERS:
        return parts[1]
    return None


def _parse(path: str) -> ast.AST:
    with open(path, encoding="utf-8") as fh:
        return ast.parse(fh.read(), filename=path)


def _shared_files() -> List[str]:
    return [p for p in _py_files(PIPELINE) if os.path.abspath(p) != os.path.abspath(REGISTRY)]


def test_shared_pipeline_imports_no_provider_package():
    violations = []
    for path in _shared_files():
        for line, module in _imported_modules(_parse(path)):
            if _provider_of(module):
                violations.append(f"{_rel(path)}:{line} imports {module}")
    assert not violations, "src/pipeline/ must reach providers only through providers.py:\n" + "\n".join(
        sorted(set(violations))
    )


def test_shared_pipeline_names_no_provider():
    violations = []
    for path in _shared_files():
        tree = _parse(path)
        docstrings = _docstring_nodes(tree)
        for node in ast.walk(tree):
            texts = []
            if isinstance(node, ast.Name):
                texts.append(node.id)
            elif isinstance(node, ast.Attribute):
                texts.append(node.attr)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                texts.append(node.name)
            elif isinstance(node, ast.arg):
                texts.append(node.arg)
            elif isinstance(node, ast.alias):
                texts.append(node.asname or node.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                texts.append(node.module)
            elif isinstance(node, ast.keyword) and node.arg:
                texts.append(node.arg)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                texts.append(node.value)
            for text in texts:
                hits = sorted(set(_tokens(text)) & set(PROVIDERS))
                if hits:
                    violations.append(f"{_rel(path)}:{getattr(node, 'lineno', '?')} names {hits[0]!r} in {text[:60]!r}")
    assert not violations, "src/pipeline/ must not name a provider (constitution VI rule 1):\n" + "\n".join(
        sorted(set(violations))
    )


@pytest.mark.parametrize("provider", PROVIDERS)
def test_provider_packages_do_not_import_each_other(provider):
    package = os.path.join(SRC, provider)
    if not os.path.isdir(package):
        pytest.skip(f"src/{provider}/ not created yet")
    violations = []
    for path in _py_files(package):
        for line, module in _imported_modules(_parse(path)):
            other = _provider_of(module)
            if other and other != provider:
                violations.append(f"{_rel(path)}:{line} imports {module}")
    assert not violations, f"src/{provider}/ must not import another provider:\n" + "\n".join(sorted(set(violations)))


def test_no_top_level_provider_modules():
    stray = sorted(
        name
        for name in os.listdir(SRC)
        if name.endswith(".py") and name.split("_", 1)[0] in PROVIDERS
    )
    assert not stray, f"provider code belongs in src/<provider>/, found: {stray}"


def test_tokenizer_examples():
    assert "aws" in _tokens("AwsPricingDownloader")
    assert "aws" in _tokens("pricing_aws")
    assert "gcp" in _tokens("GCP_PRICING_API_KEY")
    assert "aws" not in _tokens("RawSnapshot")
    assert "aws" not in _tokens("draws")
