"""scope.py: the scope of a path is its git repository, a `.breadcrumbs` folder, or a source tree; scopes nest."""
import json
import os
import tempfile
import unittest

from breadcrumbs import scope


def touch(*parts):
    path = os.path.join(*parts)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("x")
    return path


class ScopeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = os.path.realpath(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def p(self, *parts):
        path = os.path.join(self.home, *parts)
        os.makedirs(path, exist_ok=True)
        return path

    def repo(self, *parts):
        root = self.p(*parts)
        os.makedirs(os.path.join(root, ".git"), exist_ok=True)
        return root

    def test_a_git_repository_is_the_scope_of_every_path_in_it_even_one_that_does_not_exist(self):
        r = self.repo("work", "app")
        deep = self.p("work", "app", "src", "pkg")
        for path in (r, deep, os.path.join(deep, "new", "file.py")):
            self.assertEqual(scope.nearest(path, self.home), (scope.GIT, r), path)

    def test_with_no_repository_marker_or_project_file_the_folder_itself_is_the_project(self):
        notes = self.p("notes", "2026")
        self.assertEqual(scope.nearest(notes, self.home), (scope.DIR, notes))
        self.assertEqual(scope.chain(notes, self.home), [(scope.DIR, notes)])
        self.assertEqual(scope.codebase_of(os.path.join(notes, "not", "made", "yet"), self.home), notes)  # nearest folder that exists
        self.assertNotEqual(scope.scope_id(notes), scope.scope_id(self.p("notes")))  # a sibling folder is another project

    def test_a_source_tree_is_a_scope_when_no_repository_holds_it(self):
        tree = self.p("scratch", "tool")
        touch(tree, "pyproject.toml")
        self.assertEqual(scope.nearest(os.path.join(tree, "src"), self.home), (scope.SOURCE, tree))

    def test_a_repository_wins_over_a_source_tree_inside_it(self):
        r = self.repo("work", "mono")
        touch(r, "pkg", "package.json")
        self.assertEqual(scope.nearest(os.path.join(r, "pkg"), self.home), (scope.GIT, r))

    def test_a_linked_worktree_and_a_submodule_are_their_main_repository(self):
        main = self.repo("work", "main")
        os.makedirs(os.path.join(main, ".git", "worktrees", "wt"))
        wt = self.p("work", "wt")
        with open(os.path.join(wt, ".git"), "w", encoding="utf-8") as f:
            f.write("gitdir: %s\n" % os.path.join(main, ".git", "worktrees", "wt"))
        self.assertEqual(scope.codebase_of(wt, self.home), main)
        sub = self.p("work", "main", "vendor", "lib")
        os.makedirs(os.path.join(main, ".git", "modules", "lib"))
        with open(os.path.join(sub, ".git"), "w", encoding="utf-8") as f:
            f.write("gitdir: ../../.git/modules/lib\n")
        self.assertEqual(scope.codebase_of(sub, self.home), main)

    def test_the_explicit_marker_wins_over_the_repository_and_works_outside_home(self):
        r = self.repo("work", "mono")
        svc = self.p("work", "mono", "svc")
        touch(svc, ".breadcrumbs")
        self.assertEqual(scope.chain(os.path.join(svc, "src"), self.home), [(scope.EXPLICIT, svc), (scope.GIT, r)])
        with tempfile.TemporaryDirectory() as outside:
            outside = os.path.realpath(outside)
            os.makedirs(os.path.join(outside, ".breadcrumbs"))
            self.assertEqual(scope.nearest(outside, self.home), (scope.EXPLICIT, outside))

    def test_nested_repositories_nest_and_the_nearest_comes_first(self):
        outer = self.repo("work", "outer")
        inner = self.repo("work", "outer", "third_party", "inner")
        self.assertEqual([kr[1] for kr in scope.chain(inner, self.home)], [inner, outer])
        self.assertEqual(scope.codebase_of(os.path.join(outer, "src"), self.home), outer)

    def test_a_repository_is_the_project_wherever_it_is_even_in_a_hidden_folder_or_outside_home(self):
        hidden = self.repo(".pyenv", "versions", "x")
        self.assertEqual(scope.nearest(hidden, self.home), (scope.GIT, hidden))
        with tempfile.TemporaryDirectory() as outside:
            outside = os.path.realpath(outside)
            os.makedirs(os.path.join(outside, ".git"))
            self.assertEqual(scope.nearest(outside, self.home), (scope.GIT, outside))

    def test_a_breadcrumbs_file_that_says_off_means_no_project_here(self):
        r = self.repo("work", "app")
        quiet = self.p("work", "app", "vendor")
        with open(os.path.join(quiet, scope.MARKER), "w", encoding="utf-8") as f:
            f.write("off\n")
        deep = self.p("work", "app", "vendor", "lib")
        self.assertEqual(scope.chain(deep, self.home), [])  # the nearest marker says off: no project, not even the repo above
        self.assertEqual(scope.nearest(os.path.join(r, "src"), self.home), (scope.GIT, r))  # elsewhere in the repo: the repo
        inner = self.repo("work", "app", "vendor", "inner")
        self.assertEqual(scope.chain(inner, self.home), [(scope.GIT, inner)])  # a nearer repository wins, nothing above it
        same = self.repo("work", "both")
        with open(os.path.join(same, scope.MARKER), "w", encoding="utf-8") as f:
            f.write('{"scope": "off"}')
        self.assertEqual(scope.chain(same, self.home), [])  # in one folder the marker beats the repository

    def test_hermes_own_files_can_be_excluded(self):
        hermes = self.repo(".hermes-src")
        r = self.repo("work", "app")
        self.assertEqual(scope.codebase_of(r, self.home, exclude=[hermes]), r)
        shown = self.repo("visible", "platform")
        self.assertIsNone(scope.codebase_of(shown, self.home, exclude=[shown]))

    def test_the_store_folder_is_stable_per_real_path_and_distinct_for_two_folders_of_one_name(self):
        a, b = self.repo("a", "app"), self.repo("b", "app")
        base = os.path.join(self.home, ".hermes", "breadcrumbs")
        self.assertEqual(scope.store_home(base, a), scope.store_home(base, a))
        self.assertNotEqual(scope.store_home(base, a), scope.store_home(base, b))
        self.assertTrue(scope.store_home(base, a).startswith(os.path.join(base, "scopes", "app-")))
        link = os.path.join(self.home, "link-to-a")
        os.symlink(a, link)
        self.assertEqual(scope.scope_id(link), scope.scope_id(a))  # the real path names it

    def test_ensure_writes_scope_json_and_known_lists_the_scopes(self):
        r = self.repo("work", "app")
        base = os.path.join(self.home, "bc")
        folder = scope.ensure(base, scope.GIT, r)
        with open(os.path.join(folder, scope.META), encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"id": scope.scope_id(r), "root": r, "kind": "git"})
        self.assertEqual(scope.ensure(base, scope.GIT, r), folder)  # again: the same folder, nothing rewritten
        got = scope.known(base)
        self.assertEqual([(s["id"], s["root"], s["kind"], s["present"]) for s in got],
                         [(scope.scope_id(r), r, "git", False)])
        touch(folder, "index.db")
        self.assertTrue(scope.known(base)[0]["present"])

    def test_short_shows_a_path_under_home_as_tilde(self):
        self.assertEqual(scope.short(os.path.join(self.home, "work", "app"), self.home), "~/work/app")
        self.assertEqual(scope.short("/opt/x", self.home), "/opt/x")


class CoreSeesScopesTest(unittest.TestCase):
    def test_the_core_lists_a_scope_that_has_a_store_under_its_id(self):
        from breadcrumbs.core import Stores
        with tempfile.TemporaryDirectory() as root:
            base = os.path.join(root, "breadcrumbs")
            repo = os.path.join(root, "work", "app")
            os.makedirs(os.path.join(repo, ".git"))
            folder = scope.ensure(base, scope.GIT, repo)
            self.assertNotIn(scope.scope_id(repo), Stores(root=root).names())  # no index.db yet: not present
            touch(folder, "index.db")
            names = Stores(root=root).present()
            self.assertEqual(names.get(scope.scope_id(repo)), folder)


if __name__ == "__main__":
    unittest.main()
