from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "mixapi"
MIGRATION = PACKAGE / "migration"
STATE_MODULES = (
    "control_plane.py",
    "idempotency.py",
    "quota.py",
    "budget.py",
    "circuits.py",
    "route_decisions.py",
    "usage.py",
)
LEGACY_RUNTIME_NAMES = (
    "SQLiteDatabase",
    "default_catalog",
    "MIXAPI_OPENAI_BASE_URL",
    "MIXAPI_ANTHROPIC_BASE_URL",
    "MIXAPI_GEMINI_BASE_URL",
    "MIXAPI_OLLAMA_BASE_URL",
)


def test_sqlite_is_confined_to_the_migration_package() -> None:
    offenders: list[str] = []
    for path in PACKAGE.rglob("*.py"):
        if path.is_relative_to(MIGRATION):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any(_imports_sqlite(node) for node in ast.walk(tree)):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_legacy_runtime_symbols_and_modules_are_removed() -> None:
    assert not (PACKAGE / "persistence.py").exists()
    assert not (PACKAGE / "catalog.py").exists()
    offenders: dict[str, list[str]] = {}
    for path in PACKAGE.rglob("*.py"):
        if path.is_relative_to(MIGRATION):
            continue
        source = path.read_text(encoding="utf-8")
        names = [name for name in LEGACY_RUNTIME_NAMES if name in source]
        if names:
            offenders[str(path.relative_to(ROOT))] = names
    assert offenders == {}


def test_app_has_no_legacy_or_provider_specific_base_url_arguments() -> None:
    tree = ast.parse((PACKAGE / "app.py").read_text(encoding="utf-8"))
    create_app = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "create_app"
    )
    arguments = {
        argument.arg
        for argument in (*create_app.args.posonlyargs, *create_app.args.args)
    }
    assert "database_path" not in arguments
    assert not any(name.endswith("_base_url") for name in arguments)


def test_state_contract_modules_have_no_in_memory_implementations() -> None:
    offenders: dict[str, list[str]] = {}
    for filename in STATE_MODULES:
        path = PACKAGE / filename
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        classes = [
            node.name
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name.startswith("InMemory")
        ]
        if classes:
            offenders[filename] = classes
    assert offenders == {}


def test_application_does_not_construct_in_memory_state_services() -> None:
    tree = ast.parse((PACKAGE / "app.py").read_text(encoding="utf-8"))
    constructed = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id.startswith("InMemory")
        and node.func.id != "InMemoryObservability"
    }
    assert constructed == set()


def _imports_sqlite(node: ast.AST) -> bool:
    if isinstance(node, ast.Import):
        return any(alias.name == "sqlite3" for alias in node.names)
    if isinstance(node, ast.ImportFrom):
        return node.module == "sqlite3"
    return False
