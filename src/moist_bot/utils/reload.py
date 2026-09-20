from __future__ import annotations

import ast
import importlib
import sys
from graphlib import TopologicalSorter
from importlib.abc import InspectLoader
from importlib.util import resolve_name
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import ModuleType


def _imports(node: ast.AST) -> Iterator[ast.Import | ast.ImportFrom]:
    match node:
        case ast.Import() | ast.ImportFrom():
            yield node
            return
        case ast.FunctionDef() | ast.AsyncFunctionDef() | ast.ClassDef():
            return
        case ast.If(
            test=ast.Name(id='TYPE_CHECKING') | ast.Attribute(attr='TYPE_CHECKING'),
            orelse=statements,
        ):
            for statement in statements:
                yield from _imports(statement)
            return
        case _:
            for child in ast.iter_child_nodes(node):
                yield from _imports(child)


def _dependencies(module: ModuleType, names: set[str]) -> set[str]:
    loader = module.__loader__
    if not isinstance(loader, InspectLoader):
        error = f'Cannot inspect source for {module.__name__}'
        raise TypeError(error)
    source = loader.get_source(module.__name__)
    if source is None:
        error = f'No Python source for {module.__name__}'
        raise ValueError(error)

    dependencies: set[str] = set()
    for statement in _imports(ast.parse(source)):
        match statement:
            case ast.Import(names=aliases):
                dependencies.update(alias.name for alias in aliases)
            case ast.ImportFrom(module=module_name, level=level, names=aliases):
                name = module_name or ''
                if level:
                    name = resolve_name('.' * level + name, module.__package__ or '')
                for alias in aliases:
                    imported_name = f'{name}.{alias.name}'
                    dependencies.add(imported_name if imported_name in names else name)

    # Refresh package exports after all of their loaded descendants
    if (
        module.__spec__ is not None
        and module.__spec__.submodule_search_locations is not None
    ):
        dependencies.update(
            name for name in names if name.startswith(f'{module.__name__}.')
        )
    return (dependencies & names) - {module.__name__}


def deep_reload(package: ModuleType) -> ModuleType:
    """Reload a Python package and its loaded submodules in dependency order.

    Static imports at module scope determine ordering, excluding conventional
    TYPE_CHECKING blocks. Dynamic and function-local imports are not inspected.
    External modules and existing instances are unchanged.
    Dependency cycles raise graphlib.CycleError before any module is reloaded.
    An error during reload can leave the package partially updated.
    """
    if package.__spec__ is None or package.__spec__.submodule_search_locations is None:
        error = f'{package.__name__} is not a package'
        raise ValueError(error)
    if sys.modules.get(package.__name__) is not package:
        error = f'{package.__name__} is not the currently loaded package'
        raise ValueError(error)

    prefix = f'{package.__name__}.'
    loaded_modules: dict[str, ModuleType | None] = dict(sys.modules)
    modules = {
        name: module
        for name, module in loaded_modules.items()
        if module is not None and (name == package.__name__ or name.startswith(prefix))
    }
    names = set(modules)
    dependencies = {
        name: _dependencies(module, names) for name, module in modules.items()
    }
    order = tuple(TopologicalSorter(dependencies).static_order())

    importlib.invalidate_caches()

    for name in order:
        importlib.reload(modules[name])

    return package
