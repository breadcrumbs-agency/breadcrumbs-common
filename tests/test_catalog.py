"""The model dictionary: names that read alike, CRUD, clones, links between vendors, the layers of a request."""
import contextlib
import io
import json
import os
import tempfile
import types
import unittest

from breadcrumbs import catalog, dials


class Names(unittest.TestCase):
    def test_one_model_on_many_routes_reads_the_same(self):
        same = ["claude-haiku-5-5", "claude-haiku-5.5", "anthropic/claude-haiku-5.5", "Claude-Haiku-5-5-20261113",
                "claude-haiku-5-5-latest", "us.anthropic.claude-haiku-5-5-v1:0", "anthropic/claude-haiku-5.5:free",
                "claude-5-5-haiku"]
        self.assertEqual({catalog.family_key(n) for n in same}, {("5", "5", "claude", "haiku")})

    def test_other_models_stay_apart(self):
        self.assertNotEqual(catalog.family_key("claude-haiku-5-5"), catalog.family_key("claude-haiku-4-5"))
        self.assertNotEqual(catalog.family_key("llama3.1:8b"), catalog.family_key("llama3.1:70b"))
        self.assertNotEqual(catalog.family_key("qwen3-coder"), catalog.family_key("qwen3-coder-instruct"))

    def test_a_glob_has_no_family(self):
        self.assertIsNone(catalog.family_key("*haiku-5[.-]5*"))
        self.assertIsNone(catalog.family_key(""))

    def test_ids_need_a_vendor(self):
        self.assertEqual(catalog.split_id("OpenRouter/anthropic/claude-x"), ("openrouter", "anthropic/claude-x"))
        for bad in ("claude-x", "/x", "v/", ""):
            with self.assertRaises(ValueError):
                catalog.split_id(bad)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def cfg(self):
        return dials.config(self.root)

    def raw(self):
        with open(os.path.join(self.root, "config.json"), encoding="utf-8") as f:
            return json.load(f)


class Crud(Base):
    def test_create_read_update_delete(self):
        v = catalog.add(self.root, "anthropic/claude-haiku-5-5", {"ace_tokens": 90000, "temperature": 0.7}, "cheap")
        self.assertEqual(v["settings"], {"ace_tokens": 90000, "temperature": 0.7})
        self.assertEqual(v["note"], "cheap")
        with self.assertRaises(ValueError):  # a second create of the same id
            catalog.add(self.root, "anthropic/claude-haiku-5-5")
        catalog.set_value(self.root, "anthropic/claude-haiku-5-5", "ace_fold_to", 0.8)
        self.assertEqual(self.raw()["catalog"]["entries"]["anthropic/claude-haiku-5-5"]["settings"]["ace_fold_to"], 0.8)
        catalog.unset(self.root, "anthropic/claude-haiku-5-5", "temperature")
        self.assertNotIn("temperature", catalog.view(self.cfg(), "anthropic/claude-haiku-5-5")["settings"])
        catalog.remove(self.root, "anthropic/claude-haiku-5-5")
        self.assertEqual(catalog.rows(self.cfg()), [])

    def test_a_bad_value_is_refused_and_nothing_is_written(self):
        for key, value in (("ace_tokens", "many"), ("ace_tokens", 0), ("ace_typo", 1), ("temperature", object())):
            with self.assertRaises(ValueError):
                catalog.add(self.root, "anthropic/x", {key: value})
        self.assertFalse(os.path.exists(os.path.join(self.root, "config.json")))
        with self.assertRaises(KeyError):
            catalog.set_value(self.root, "anthropic/missing", "temperature", 0.1)

    def test_other_keys_of_the_file_are_kept(self):
        with open(os.path.join(self.root, "config.json"), "w", encoding="utf-8") as f:
            json.dump({"hive": {"ms": 150}, "models": {"gpt-*": {"temperature": 0.2}}}, f)
        catalog.add(self.root, "openai/gpt-5", {"temperature": 0.1})
        got = self.raw()
        self.assertEqual(got["hive"], {"ms": 150})
        self.assertEqual(got["models"], {"gpt-*": {"temperature": 0.2}})

    def test_a_bad_file_is_never_overwritten(self):
        path = os.path.join(self.root, "config.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not json")
        with self.assertRaises(ValueError):
            catalog.add(self.root, "anthropic/x")
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "{not json")


class Bonds(Base):
    def setUp(self):
        super().setUp()
        catalog.add(self.root, "anthropic/claude-haiku-5-5", {"ace_tokens": 90000, "temperature": 0.7})

    def test_family_is_made_for_you(self):
        catalog.add(self.root, "openrouter/anthropic/claude-haiku-5.5", {"temperature": 0.3})
        v = catalog.view(self.cfg(), "openrouter/anthropic/claude-haiku-5.5")
        self.assertEqual(v["family"], ["anthropic/claude-haiku-5-5"])
        # its own temperature wins; the dial it does not set comes from the family
        self.assertEqual(dials.value(self.cfg(), "anthropic/claude-haiku-5.5", "ace_tokens", {},
                                     provider="openrouter"), 90000)
        self.assertEqual(dials.options(self.cfg(), "anthropic/claude-haiku-5.5", "openrouter")["temperature"], 0.3)

    def test_a_vendor_with_no_entry_reads_the_family(self):
        # no entry for nous: the request still reads the settings of the model that reads alike
        self.assertEqual(dials.options(self.cfg(), "claude-haiku-5.5", "nous"),
                         {"ace_tokens": 90000, "temperature": 0.7})
        rows = {r.dial.key: r for r in dials.resolve(self.cfg(), "claude-haiku-5.5", {}, "nous")}
        self.assertEqual(rows["ace_tokens"].source, 'catalog["anthropic/claude-haiku-5-5"] (family)')

    def test_no_provider_fits_any_vendor(self):
        self.assertEqual(dials.value(self.cfg(), "claude-haiku-5-5", "ace_tokens", {}), 90000)

    def test_the_vendor_of_an_entry_counts_for_its_own_layer(self):
        catalog.add(self.root, "bedrock/claude-haiku-5-5", {"temperature": 0.1})
        self.assertEqual(dials.options(self.cfg(), "claude-haiku-5-5", "bedrock")["temperature"], 0.1)
        self.assertEqual(dials.options(self.cfg(), "claude-haiku-5-5", "anthropic")["temperature"], 0.7)

    def test_unlink_vetoes_a_family_and_relate_lifts_it(self):
        catalog.add(self.root, "openrouter/anthropic/claude-haiku-5.5", {"temperature": 0.3})
        catalog.unlink(self.root, "openrouter/anthropic/claude-haiku-5.5", "anthropic/claude-haiku-5-5")
        self.assertEqual(dials.options(self.cfg(), "anthropic/claude-haiku-5.5", "openrouter"), {"temperature": 0.3})
        self.assertEqual(catalog.view(self.cfg(), "anthropic/claude-haiku-5-5")["unlinked"],
                         ["openrouter/anthropic/claude-haiku-5.5"])
        catalog.relate(self.root, "anthropic/claude-haiku-5-5", "openrouter/anthropic/claude-haiku-5.5")
        self.assertEqual(dials.value(self.cfg(), "anthropic/claude-haiku-5.5", "ace_tokens", {}, "openrouter"), 90000)

    def test_link_ties_names_that_do_not_read_alike(self):
        catalog.add(self.root, "ollama/haiku-local", {"ace_ceiling": 50000})
        self.assertEqual(dials.options(self.cfg(), "haiku-local", "ollama"), {"ace_ceiling": 50000})
        catalog.link(self.root, "ollama/haiku-local", "anthropic/claude-haiku-5-5")
        got = dials.options(self.cfg(), "haiku-local", "ollama")
        self.assertEqual(got, {"ace_tokens": 90000, "temperature": 0.7, "ace_ceiling": 50000})  # own settings win
        # and the bond runs both ways
        self.assertEqual(dials.options(self.cfg(), "claude-haiku-5-5", "anthropic")["ace_ceiling"], 50000)

    def test_a_link_holds_through_a_third_entry(self):
        catalog.add(self.root, "ollama/a", {"top_k": 20})
        catalog.add(self.root, "vllm/b", {"top_p": 0.9})
        catalog.link(self.root, "ollama/a", "vllm/b")
        catalog.link(self.root, "vllm/b", "anthropic/claude-haiku-5-5")
        self.assertEqual(catalog.view(self.cfg(), "ollama/a")["links"], ["anthropic/claude-haiku-5-5", "vllm/b"])

    def test_link_needs_both_entries_and_two_of_them(self):
        with self.assertRaises(KeyError):
            catalog.link(self.root, "anthropic/claude-haiku-5-5", "ollama/none")
        with self.assertRaises(ValueError):
            catalog.link(self.root, "anthropic/claude-haiku-5-5", "anthropic/claude-haiku-5-5")

    def test_remove_cuts_its_links(self):
        catalog.add(self.root, "ollama/a")
        catalog.link(self.root, "ollama/a", "anthropic/claude-haiku-5-5")
        self.assertEqual(catalog.remove(self.root, "ollama/a"), ["anthropic/claude-haiku-5-5"])
        self.assertNotIn("links", self.raw()["catalog"])

    def test_the_models_globs_keep_the_last_word_of_the_file(self):
        raw = self.raw()
        raw["models"] = {"*haiku*": {"temperature": 0.05}}
        with open(os.path.join(self.root, "config.json"), "w", encoding="utf-8") as f:
            json.dump(raw, f)
        self.assertEqual(dials.options(self.cfg(), "claude-haiku-5-5", "anthropic")["temperature"], 0.05)
        self.assertEqual(dials.value(self.cfg(), "claude-haiku-5-5", "ace_tokens", {}), 90000)

    def test_a_scope_file_over_the_profile_file(self):
        scope = tempfile.TemporaryDirectory()
        self.addCleanup(scope.cleanup)
        catalog.add(scope.name, "anthropic/claude-haiku-5-5", {"ace_tokens": 120000})
        chain = [("profile", dials.config(self.root)), ("scope", dials.config(scope.name))]
        self.assertEqual(dials.value(chain, "claude-haiku-5-5", "ace_tokens", {}, "anthropic"), 120000)
        self.assertEqual(dials.options(chain, "claude-haiku-5-5", "anthropic")["temperature"], 0.7)

    def test_a_glob_entry_covers_every_vendor_by_name(self):
        catalog.add(self.root, "*/claude-*", {"top_k": 7})
        self.assertEqual(dials.options(self.cfg(), "claude-sonnet-5", "bedrock")["top_k"], 7)
        # exact beats the glob
        catalog.add(self.root, "*/claude-sonnet-5", {"top_k": 9})
        self.assertEqual(dials.options(self.cfg(), "claude-sonnet-5", "bedrock")["top_k"], 9)


class Dates(Base):
    """The implementation date decides the master of a family or a link group: the newest implementation wins."""

    def setUp(self):
        super().setUp()
        self.stamps = iter("2026-11-20T09:00:%02d" % n for n in range(60))
        saved = catalog.now
        catalog.now = lambda: next(self.stamps)
        self.addCleanup(lambda: setattr(catalog, "now", saved))

    def test_the_newest_member_of_a_family_is_the_master_whatever_the_ids(self):
        catalog.add(self.root, "zeta/model-1.5", {"temperature": 0.1})      # id sorts last, written first
        catalog.add(self.root, "alpha/model-1-5", {"temperature": 0.9})     # id sorts first, written second
        self.assertEqual(catalog.master(self.cfg(), "zeta/model-1.5"), "alpha/model-1-5")
        self.assertEqual(dials.options(self.cfg(), "model-1-5", "other")["temperature"], 0.9)
        rows = {r.dial.key: r for r in dials.resolve(self.cfg(), "model-1-5", {}, "other")}
        self.assertEqual(rows["ace_tokens"].source, "default")

    def test_a_change_makes_the_entry_the_newest(self):
        catalog.add(self.root, "a/model-1", {"temperature": 0.1})
        catalog.add(self.root, "b/model-1", {"temperature": 0.2})
        self.assertEqual(dials.options(self.cfg(), "model-1", "c")["temperature"], 0.2)
        catalog.set_value(self.root, "a/model-1", "temperature", 0.3)
        self.assertEqual(dials.options(self.cfg(), "model-1", "c")["temperature"], 0.3)
        self.assertEqual(catalog.view(self.cfg(), "b/model-1")["master"], "a/model-1")

    def test_since_names_the_implementation_and_beats_the_stamp_of_a_change(self):
        catalog.add(self.root, "a/model-1", {"temperature": 0.1}, since="2026-12-01")
        catalog.add(self.root, "b/model-1", {"temperature": 0.2})            # written later, but a/ is dated later
        self.assertEqual(catalog.view(self.cfg(), "a/model-1")["date"], "2026-12-01")
        self.assertEqual(catalog.master(self.cfg(), "b/model-1"), "a/model-1")
        catalog.set_since(self.root, "a/model-1", "2026-01-01")
        self.assertEqual(catalog.master(self.cfg(), "a/model-1"), "b/model-1")
        catalog.set_since(self.root, "a/model-1", "")
        self.assertEqual(catalog.view(self.cfg(), "a/model-1")["since"], "")
        with self.assertRaises(ValueError):
            catalog.set_since(self.root, "a/model-1", "yesterday")
        with self.assertRaises(ValueError):
            catalog.add(self.root, "c/model-1", since="11/20/2026")

    def test_a_note_or_a_link_does_not_change_the_date(self):
        catalog.add(self.root, "a/model-1")
        before = catalog.view(self.cfg(), "a/model-1")["updated"]
        catalog.set_note(self.root, "a/model-1", "x")
        self.assertEqual(catalog.view(self.cfg(), "a/model-1")["updated"], before)

    def test_a_link_group_reads_oldest_to_newest(self):
        catalog.add(self.root, "m/own", {})
        catalog.add(self.root, "z/old", {"top_k": 1})
        catalog.add(self.root, "a/new", {"top_k": 2})
        catalog.link(self.root, "m/own", "z/old")
        catalog.link(self.root, "m/own", "a/new")
        self.assertEqual(dials.options(self.cfg(), "own", "m")["top_k"], 2)
        self.assertEqual(catalog.master(self.cfg(), "m/own"), "a/new")

    def test_the_master_of_an_unlinked_family_member_is_itself(self):
        catalog.add(self.root, "a/model-1")
        catalog.add(self.root, "b/model-1")
        catalog.unlink(self.root, "a/model-1", "b/model-1")
        self.assertEqual(catalog.master(self.cfg(), "a/model-1"), "a/model-1")

    def test_a_clone_dates_from_now_and_leaves_the_since_behind(self):
        catalog.add(self.root, "a/model-1", {"top_k": 1}, since="2026-01-01")
        v = catalog.clone(self.root, "a/model-1", "b")
        self.assertEqual((v["since"], v["date"] > "2026-11-20"), ("", True))
        self.assertEqual(v["master"], "b/model-1")

    def test_the_command_sets_and_shows_the_date(self):
        args = types.SimpleNamespace(action="add", items=["a/model-1", "top_k=1"], note=None, link=False, force=False,
                                     since="2026-11-15")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(catalog.command(args, self.root), 0)
        self.assertIn("date        2026-11-15  (since)", out.getvalue())
        args = types.SimpleNamespace(action="since", items=["a/model-1", "-"], note=None, link=False, force=False)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(catalog.command(args, self.root), 0)
        self.assertIn("(updated)", out.getvalue())


class Export(Base):
    """A dictionary moves as a document: nothing assigns it a file name."""

    def setUp(self):
        super().setUp()
        catalog.add(self.root, "anthropic/claude-haiku-5-5", {"ace_tokens": 90000}, since="2026-11-13")
        catalog.add(self.root, "ollama/haiku-local", {"top_k": 20})
        catalog.link(self.root, "ollama/haiku-local", "anthropic/claude-haiku-5-5")

    def other(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return tmp.name

    def test_export_then_import_makes_the_same_dictionary_with_its_dates(self):
        doc = catalog.export(self.cfg())
        self.assertEqual((doc["format"], sorted(doc["entries"]), doc["links"]),
                         ("breadcrumbs-model-dictionary", ["anthropic/claude-haiku-5-5", "ollama/haiku-local"],
                          [["anthropic/claude-haiku-5-5", "ollama/haiku-local"]]))
        there = self.other()
        self.assertEqual(catalog.import_document(there, json.loads(json.dumps(doc))), sorted(doc["entries"]))
        self.assertEqual(catalog.export(dials.config(there)), doc)
        self.assertEqual(catalog.view(dials.config(there), "anthropic/claude-haiku-5-5")["date"], "2026-11-13")

    def test_import_refuses_an_id_that_is_there_unless_forced_and_writes_nothing_on_a_bad_document(self):
        doc = catalog.export(self.cfg())
        with self.assertRaises(ValueError):
            catalog.import_document(self.root, doc)
        doc["entries"]["anthropic/claude-haiku-5-5"]["settings"]["ace_tokens"] = 80000
        catalog.import_document(self.root, doc, force=True)
        self.assertEqual(catalog.view(self.cfg(), "anthropic/claude-haiku-5-5")["settings"], {"ace_tokens": 80000})
        there = self.other()
        for bad in ({}, {"format": "x"}, {"format": catalog.FORMAT, "entries": {"nope": {}}},
                    {"format": catalog.FORMAT, "entries": {"a/b": {"settings": {"ace_tokens": -1}}}},
                    {"format": catalog.FORMAT, "entries": {"a/b": {"since": "soon"}}},
                    {"format": catalog.FORMAT, "entries": {"a/b": {}}, "links": [["a/b", "c/d"]]}):
            with self.assertRaises(ValueError):
                catalog.import_document(there, bad)
        self.assertFalse(os.path.exists(os.path.join(there, "config.json")))

    def test_the_command_prints_the_document_and_writes_a_file_only_where_one_is_named(self):
        def run(action, *items, **kw):
            args = types.SimpleNamespace(action=action, items=list(items), note=None, link=False,
                                         force=kw.get("force", False), since=None)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = catalog.command(args, kw.get("home", self.root))
            return code, out.getvalue()
        code, text = run("export")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(text)["format"], catalog.FORMAT)
        self.assertEqual(sorted(os.listdir(self.root)), ["config.json"])      # printing made no file
        named = os.path.join(self.other(), "mine.json")
        self.assertEqual(run("export", named)[0], 0)
        there = self.other()
        code, out = run("import", named, home=there)
        self.assertEqual(code, 0)
        self.assertIn("imported 2 entries", out)
        self.assertEqual(catalog.export(dials.config(there)), json.loads(text))

    def test_the_widget_exports_without_a_file_and_imports_a_document(self):
        got = catalog.apply(self.root, "export")
        self.assertEqual(got["export"]["format"], catalog.FORMAT)
        there = self.other()
        got = catalog.apply(there, "import", {"document": got["export"]})
        self.assertEqual(len(got["entries"]), 2)


class Clone(Base):
    def setUp(self):
        super().setUp()
        catalog.add(self.root, "anthropic/claude-haiku-5-5", {"ace_tokens": 90000})

    def test_clone_to_a_vendor_keeps_the_model(self):
        v = catalog.clone(self.root, "anthropic/claude-haiku-5-5", "nous")
        self.assertEqual(v["id"], "nous/claude-haiku-5-5")
        self.assertEqual(v["settings"], {"ace_tokens": 90000})
        self.assertEqual(v["cloned_from"], "anthropic/claude-haiku-5-5")

    def test_the_copy_changes_alone(self):
        catalog.clone(self.root, "anthropic/claude-haiku-5-5", "bedrock/claude-haiku-5-5-v1")
        catalog.set_value(self.root, "bedrock/claude-haiku-5-5-v1", "ace_tokens", 50000)
        self.assertEqual(catalog.view(self.cfg(), "anthropic/claude-haiku-5-5")["settings"], {"ace_tokens": 90000})

    def test_clone_refuses_to_replace_and_force_replaces(self):
        catalog.add(self.root, "nous/claude-haiku-5-5", {"ace_tokens": 1000})
        with self.assertRaises(ValueError):
            catalog.clone(self.root, "anthropic/claude-haiku-5-5", "nous")
        v = catalog.clone(self.root, "anthropic/claude-haiku-5-5", "nous", force=True)
        self.assertEqual(v["settings"], {"ace_tokens": 90000})

    def test_clone_with_link_ties_the_pair_and_a_missing_source_is_refused(self):
        v = catalog.clone(self.root, "anthropic/claude-haiku-5-5", "ollama/haiku-local", link=True)
        self.assertEqual(v["links"], ["anthropic/claude-haiku-5-5"])
        with self.assertRaises(KeyError):
            catalog.clone(self.root, "anthropic/none", "nous")
        with self.assertRaises(ValueError):
            catalog.clone(self.root, "anthropic/claude-haiku-5-5", "anthropic/claude-haiku-5-5")


class Discovery(Base):
    def test_suggest_shows_the_family_and_the_near(self):
        catalog.add(self.root, "anthropic/claude-haiku-5-5")
        catalog.add(self.root, "openrouter/anthropic/claude-haiku-5.5")
        catalog.add(self.root, "ollama/claude-haiku-5-5-distill")
        got = catalog.suggest(self.cfg())
        self.assertEqual(got[0]["kind"], "family")
        self.assertEqual({got[0]["a"], got[0]["b"]}, {"anthropic/claude-haiku-5-5",
                                                      "openrouter/anthropic/claude-haiku-5.5"})
        self.assertTrue(any(s["kind"] == "near" and "ollama/claude-haiku-5-5-distill" in (s["a"], s["b"]) for s in got))
        catalog.unlink(self.root, "anthropic/claude-haiku-5-5", "openrouter/anthropic/claude-haiku-5.5")
        self.assertFalse(any(s["kind"] == "family" for s in catalog.suggest(self.cfg())))

    def test_seen_models_that_have_no_entry_show_what_they_would_read(self):
        catalog.add(self.root, "anthropic/claude-haiku-5-5", {"ace_tokens": 90000})
        with open(os.path.join(self.root, "usage.log"), "w", encoding="utf-8") as f:
            f.write("2026-11-20T07:26:02 sess=s task=main model=claude-haiku-5.5 resp=x prov=nous in=1\n"
                    "2026-11-20T07:27:02 sess=s task=main model=claude-haiku-5-5 resp=x prov=anthropic in=1\n"
                    "2026-11-20T07:28:02 sess=s task=aux model=gemma-3 resp=x prov=ollama in=1\n"
                    "garbage\n")
        got = catalog.unlisted(self.cfg(), catalog.seen(self.root))
        by = {g["id"]: g for g in got}
        self.assertNotIn("anthropic/claude-haiku-5-5", by)  # covered
        self.assertEqual(by["nous/claude-haiku-5.5"]["inherits"], [{"id": "anthropic/claude-haiku-5-5", "how": "family"}])
        self.assertEqual(by["ollama/gemma-3"]["inherits"], [])

    def test_problems_are_named_for_the_dials_command(self):
        with open(os.path.join(self.root, "config.json"), "w", encoding="utf-8") as f:
            json.dump({"catalog": {"entries": {"nope": {}, "a/b": {"settings": {"ace_tokens": -1}}}, "links": [["a/b", "c/d"]]}}, f)
        notes = dials.unknown(self.cfg())
        self.assertTrue(any('catalog["nope"]' in n for n in notes))
        self.assertTrue(any('catalog["a/b"] ace_tokens' in n for n in notes))
        self.assertTrue(any("catalog links" in n for n in notes))
        self.assertNotIn("config catalog: no such dial", notes)

    def test_the_widget_lists_the_catalog_and_applies_an_action(self):
        got = catalog.apply(self.root, "add", {"id": "anthropic/x", "settings": {"top_k": 3}})
        self.assertEqual([e["id"] for e in got["entries"]], ["anthropic/x"])
        got = catalog.apply(self.root, "clone", {"id": "anthropic/x", "to": "nous"})
        self.assertEqual([e["id"] for e in got["entries"]], ["anthropic/x", "nous/x"])
        self.assertEqual(dials.settings(self.root, environ={})["catalog"], ["anthropic/x", "nous/x"])
        with self.assertRaises(ValueError):
            catalog.apply(self.root, "explode", {})


class Command(Base):
    def run_cmd(self, action, *items, **kw):
        args = types.SimpleNamespace(action=action, items=list(items), note=kw.get("note"),
                                     link=kw.get("link", False), force=kw.get("force", False))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = catalog.command(args, self.root)
        return code, out.getvalue(), err.getvalue()

    def test_the_whole_cycle(self):
        code, out, _ = self.run_cmd("add", "anthropic/claude-haiku-5-5", "ace_tokens=90000", "temperature=0.7",
                                    note="cheap")
        self.assertEqual(code, 0)
        self.assertIn("ace_tokens", out)
        self.assertEqual(self.run_cmd("set", "anthropic/claude-haiku-5-5", "top_k=20")[0], 0)
        self.assertEqual(self.run_cmd("clone", "anthropic/claude-haiku-5-5", "nous", link=True)[0], 0)
        code, out, _ = self.run_cmd("list")
        self.assertIn("nous/claude-haiku-5-5", out)
        code, out, _ = self.run_cmd("show", "nous/claude-haiku-5-5")
        self.assertIn("cloned from anthropic/claude-haiku-5-5", out)
        self.assertIn("linked to", out)
        self.assertEqual(self.run_cmd("unlink", "nous/claude-haiku-5-5", "anthropic/claude-haiku-5-5")[0], 0)
        self.assertEqual(self.run_cmd("rm", "nous/claude-haiku-5-5")[0], 0)
        self.assertEqual(self.run_cmd("suggest")[0], 0)
        self.assertEqual(self.run_cmd("seen")[0], 0)

    def test_errors_exit_nonzero_and_say_why(self):
        code, _, err = self.run_cmd("show", "nous/none")
        self.assertEqual(code, 1)
        self.assertIn("no entry nous/none", err)
        code, _, err = self.run_cmd("add", "claude-haiku")
        self.assertEqual(code, 1)
        self.assertIn("VENDOR/MODEL", err)
        self.assertEqual(self.run_cmd("show")[0], 2)

    def test_module_entry_point(self):
        import subprocess
        import sys

        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        r = subprocess.run([sys.executable, "-m", "breadcrumbs", "--home", self.root, "models", "add", "a/b", "top_k=3"],
                           capture_output=True, text=True, cwd=repo, env={**os.environ, "PYTHONPATH": repo})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("top_k", r.stdout)


if __name__ == "__main__":
    unittest.main()
