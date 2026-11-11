"""The layers of breadcrumbs: common (package `breadcrumbs`), then spacegraph (`breadcrumbs_spacegraph`), then the
harnesses (`breadcrumbs_hermes`, `breadcrumbs_claude`, `breadcrumbs_codex`, `breadcrumbs_opencode`, `breadcrumbs_grok`).
A module must not import a module of a higher layer, at any place in the file (the top, or inside a function), and a
harness package imports no other harness package. A higher layer gives its part through the port registry
(breadcrumbs/ports.py), by ONE module: its adapter, <package>/adapter.py. ports.load() imports the adapters by the name
of their package, so no module imports an adapter.

The seven folders are seven future repositories. This test reads the folders that stand beside breadcrumbs-common (all
seven in the monorepo, only common in a repository of its own)."""
import ast
import os
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # breadcrumbs-common
ROOT = os.path.dirname(HERE)  # the folder that holds the seven folders

LAYERS = (  # (layer, folder, package, rank): a module imports its own layer and the layers of a lower rank
    ("common", "breadcrumbs-common", "breadcrumbs", 0),
    ("spacegraph", "breadcrumbs-spacegraph", "breadcrumbs_spacegraph", 1),
    ("hermes", "breadcrumbs-hermes", "breadcrumbs_hermes", 2),
    ("claude", "breadcrumbs-claude", "breadcrumbs_claude", 2),
    ("codex", "breadcrumbs-codex", "breadcrumbs_codex", 2),
    ("opencode", "breadcrumbs-opencode", "breadcrumbs_opencode", 2),
    ("grok", "breadcrumbs-grok", "breadcrumbs_grok", 2),
)
ADAPTER = "adapter"
RANK = {package: rank for _layer, _folder, package, rank in LAYERS}
LAYER = {package: layer for layer, _folder, package, _rank in LAYERS}


def packages():
    """The (package, folder of the package) of every layer that is beside this folder."""
    out = []
    for _layer, folder, package, _rank in LAYERS:
        path = os.path.join(ROOT, folder, package)
        if os.path.isdir(path):
            out.append((package, path))
    return out


def modules(path):
    return sorted(f[:-3] for f in os.listdir(path) if f.endswith(".py"))


def imports(path, package="breadcrumbs"):
    """Each module that the file `path` of `package` imports from the layers: (line, package, module or None), at any
    depth. A relative import (one dot) names the package itself."""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level > 1:
                continue  # a level above the package holds no module of a layer
            if node.level == 1:
                if node.module:
                    found.append((node.lineno, package, node.module.split(".")[0]))  # from .view import View
                else:
                    found += [(node.lineno, package, a.name) for a in node.names]  # from . import view
                continue
            parts = (node.module or "").split(".")
            if parts[0] in RANK:
                if len(parts) > 1:
                    found.append((node.lineno, parts[0], parts[1]))  # from breadcrumbs.store import Store
                else:
                    found += [(node.lineno, parts[0], a.name) for a in node.names]  # from breadcrumbs import store
        elif isinstance(node, ast.Import):
            for a in node.names:
                parts = a.name.split(".")
                if parts[0] in RANK:
                    found.append((node.lineno, parts[0], parts[1] if len(parts) > 1 else None))  # import breadcrumbs.store
    return found


def wrong_way():
    """Each import of a higher layer, or of another harness layer: "package.module (layer) imports package (layer) at
    line N[, N...]", one per pair. A layer may import itself and the layers of a lower rank."""
    lines = {}
    for package, path in packages():
        for name in modules(path):
            for lineno, target, _module in imports(os.path.join(path, name + ".py"), package):
                if target != package and RANK[target] >= RANK[package]:
                    lines.setdefault((package + "." + name, package, target), []).append(lineno)
    return ["%s (%s) imports %s (%s) at line %s" % (who, LAYER[package], target, LAYER[target],
                                                   ", ".join(str(n) for n in sorted(set(where))))
            for (who, package, target), where in sorted(lines.items())]


class LayerTest(unittest.TestCase):
    def test_common_is_there_and_each_folder_holds_one_known_package(self):
        # a new package must be put in a layer above, or the rule below does not see it
        self.assertEqual([p for p, _ in packages()][:1], ["breadcrumbs"])
        for _layer, folder, package, _rank in LAYERS:
            path = os.path.join(ROOT, folder)
            if os.path.isdir(path):
                self.assertEqual(sorted(d for d in os.listdir(path) if d.startswith("breadcrumbs")
                                        and os.path.isdir(os.path.join(path, d))), [package], folder)

    def test_each_package_above_common_has_one_adapter_and_common_has_none(self):
        for package, path in packages():
            has = os.path.isfile(os.path.join(path, ADAPTER + ".py"))
            self.assertEqual(has, package != "breadcrumbs", package)
            self.assertEqual([m for m in modules(path) if m.startswith(ADAPTER)], [ADAPTER] if has else [], package)

    def test_no_module_imports_a_higher_layer(self):
        found = wrong_way()
        self.assertEqual(found, [], "%d imports point the wrong way:\n%s" % (len(found), "\n".join(found)))

    def test_no_module_imports_an_adapter(self):
        # ports.load() imports them by the name of the package. A module that imported one would reach a higher layer.
        hits = []
        for package, path in packages():
            for name in modules(path):
                hits += ["%s.%s:%d" % (package, name, line) for line, _target, module in
                         imports(os.path.join(path, name + ".py"), package) if module == ADAPTER]
        self.assertEqual(hits, [])

    def test_the_imports_inside_functions_are_seen(self):
        # the rule reads every import of a file: cli imports its command modules inside main()
        path = os.path.join(HERE, "breadcrumbs")
        self.assertIn("store", [module for _line, _package, module in imports(os.path.join(path, "cli.py"))])
        self.assertIn("cli", [module for _line, _package, module in imports(os.path.join(path, "__main__.py"))])


if __name__ == "__main__":
    unittest.main()
