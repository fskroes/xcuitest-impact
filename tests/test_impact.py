import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("impact", HERE / "impact.py")
impact = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(impact)
impact.configure({"sources": ["App/Sources", "Kit/Sources/"], "ui_tests": "App/UITests", "target": "AppUITests",
                  "scope": ["App/", "Kit/"], "ignore": ["Kit/Tests/"],
                  "read_by": {"api/openapi.json": "StandInServer.swift"}})

VIEW = "App/Sources/Screens/SellView.swift"
MODEL = "Kit/Sources/Domain/Money.swift"
MAP = {"format": 1, "commit": "c0ffee", "when": "2026-11-10T12:00:00+0100",
       "lines": {VIEW: [[10, 30]], MODEL: [[5, 9]]},
       "tests": {"SellTests/testSell": {VIEW: [[10, 20]]},
                 "SellTests/testBoth": {VIEW: [[10, 30]], MODEL: [[5, 9]]},
                 "MoneyTests/testMoney": {MODEL: [[5, 6]]}},
       "problems": {}}
CLASS_TESTS = {"SellTests": ["testSell", "testBoth"], "MoneyTests": ["testMoney"]}
FILES = {"SellTests.swift": {"classes": ["SellTests"], "tests": {"SellTests": ["testSell", "testBoth"]},
                             "names": {"SellTests"}, "words": {"SellTests", "E2ETestCase", "launch"}},
         "MoneyTests.swift": {"classes": ["MoneyTests"], "tests": {"MoneyTests": ["testMoney"]},
                              "names": {"MoneyTests"}, "words": {"MoneyTests", "XCTestCase"}},
         "E2E.swift": {"classes": ["E2ETestCase"], "tests": {}, "names": {"E2ETestCase"}, "words": {"E2ETestCase"}},
         "StandInServer.swift": {"classes": [], "tests": {}, "names": {"StandInServer"}, "words": {"StandInServer"}},
         "TestImpact.swift": {"classes": ["TestImpact"], "tests": {}, "names": {"TestImpact"}, "words": {"TestImpact"}}}
PARENTS = {"SellTests": "E2ETestCase", "MoneyTests": "XCTestCase", "E2ETestCase": "XCTestCase"}
EVERY = {"SellTests/testSell", "SellTests/testBoth", "MoneyTests/testMoney"}


def modified(old, count, removed, added):
    return {"kind": "modified", "hunks": [{"old": old, "count": count, "removed": removed, "added": added}]}


def picked(changes, test_map=MAP, class_tests=CLASS_TESTS):
    selection = impact.select(changes, test_map, class_tests, FILES, PARENTS)
    return selection["all"], set(selection["tests"])


class Diff(unittest.TestCase):
    def test_a_hunk_has_its_old_lines_and_both_texts(self):
        text = ("diff --git a/a.swift b/a.swift\nindex 1..2 100644\n--- a/a.swift\n+++ b/a.swift\n"
                "@@ -12,2 +12,3 @@ func f() {\n-    let a = 1\n-    let b = 2\n+    let a = 3\n+    // why\n+    let b = 4\n")
        self.assertEqual(impact.parse_diff(text), {"a.swift": {"kind": "modified", "hunks": [
            {"old": 12, "count": 2, "removed": ["    let a = 1", "    let b = 2"],
             "added": ["    let a = 3", "    // why", "    let b = 4"]}]}})

    def test_lines_put_in_have_count_zero_and_one_line_has_no_count(self):
        text = ("diff --git a/a.swift b/a.swift\n--- a/a.swift\n+++ b/a.swift\n@@ -7,0 +8 @@\n+new\n@@ -9 +10 @@\n-x\n+y\n")
        hunks = impact.parse_diff(text)["a.swift"]["hunks"]
        self.assertEqual([(h["old"], h["count"]) for h in hunks], [(7, 0), (9, 1)])

    def test_a_new_file_a_deleted_file_and_a_binary_file(self):
        text = ("diff --git a/new.swift b/new.swift\nnew file mode 100644\n--- /dev/null\n+++ b/new.swift\n@@ -0,0 +1 @@\n+x\n"
                "diff --git a/old.swift b/old.swift\ndeleted file mode 100644\n--- a/old.swift\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"
                "diff --git a/icon.png b/icon.png\nBinary files a/icon.png and b/icon.png differ\n")
        changes = impact.parse_diff(text)
        self.assertEqual({p: c["kind"] for p, c in changes.items()},
                         {"new.swift": "added", "old.swift": "deleted", "icon.png": "modified"})
        self.assertEqual(changes["icon.png"]["hunks"], [])

    def test_a_removed_line_that_starts_with_dashes_is_kept(self):
        text = "diff --git a/a.swift b/a.swift\n--- a/a.swift\n+++ b/a.swift\n@@ -3 +3 @@\n--x\n+-y\n"
        hunk = impact.parse_diff(text)["a.swift"]["hunks"][0]
        self.assertEqual((hunk["removed"], hunk["added"]), (["-x"], ["-y"]))

    def test_the_tree_is_compared_with_a_commit_and_new_files_count(self):
        root = Path(tempfile.mkdtemp())

        def git(*args):
            subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True,
                           capture_output=True)
        git("init", "-q")
        (root / "a.swift").write_text("one\ntwo\nthree\n")
        (root / "gone.swift").write_text("x\n")
        git("add", ".")
        git("commit", "-qm", "first")
        commit = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        (root / "a.swift").write_text("one\nTWO\nthree\n")
        (root / "gone.swift").unlink()
        (root / "new file.swift").write_text("y\n")
        changes = impact.tree_changes(commit, root)
        self.assertEqual({p: c["kind"] for p, c in changes.items()},
                         {"a.swift": "modified", "gone.swift": "deleted", "new file.swift": "added"})
        self.assertEqual(changes["a.swift"]["hunks"], [{"old": 2, "count": 1, "removed": ["two"], "added": ["TWO"]}])
        self.assertIsNone(impact.tree_changes("0" * 40, root))


class Selection(unittest.TestCase):
    def test_a_changed_line_selects_the_tests_that_ran_it(self):
        self.assertEqual(picked({VIEW: modified(25, 1, ["    total += 1"], ["    total += 2"])}),
                         (None, {"SellTests/testBoth"}))
        self.assertEqual(picked({VIEW: modified(15, 1, ["    total += 1"], ["    total += 2"])}),
                         (None, {"SellTests/testSell", "SellTests/testBoth"}))

    def test_the_line_on_each_side_of_a_change_counts(self):
        self.assertEqual(picked({VIEW: modified(21, 1, ["    a()"], ["    b()"])}),
                         (None, {"SellTests/testSell", "SellTests/testBoth"}))
        self.assertEqual(picked({VIEW: modified(22, 1, ["    a()"], ["    b()"])}), (None, {"SellTests/testBoth"}))

    def test_lines_put_in_select_the_tests_that_ran_a_line_next_to_them(self):
        self.assertEqual(picked({VIEW: modified(20, 0, [], ["    extra()"])}),
                         (None, {"SellTests/testSell", "SellTests/testBoth"}))
        self.assertEqual(picked({VIEW: modified(24, 0, [], ["    extra()"])}), (None, {"SellTests/testBoth"}))

    def test_lines_put_in_at_the_edge_of_a_body_select_every_test_that_ran_the_file(self):
        self.assertEqual(picked({VIEW: modified(30, 0, [], ["    var log = Analytics.shared.start()"])}),
                         (None, {"SellTests/testSell", "SellTests/testBoth"}))
        self.assertEqual(picked({VIEW: modified(9, 0, [], ["    func body() {}"])}),
                         (None, {"SellTests/testSell", "SellTests/testBoth"}))

    def test_a_change_of_comments_and_blank_lines_selects_nothing(self):
        self.assertEqual(picked({VIEW: modified(15, 2, ["    // old", ""], ["    // new", "    /// more", "  "])}),
                         (None, set()))

    def test_a_changed_declaration_selects_every_test_that_ran_the_file(self):
        self.assertEqual(picked({VIEW: modified(3, 1, ["    var limit: Int"], ["    var limit: Double"])}),
                         (None, {"SellTests/testSell", "SellTests/testBoth"}))
        self.assertEqual(picked({MODEL: modified(40, 0, [], ["    var note = \"\""])}),
                         (None, {"SellTests/testBoth", "MoneyTests/testMoney"}))

    def test_a_line_no_test_ran_selects_nothing(self):
        only_sell = dict(MAP, tests={"SellTests/testSell": {VIEW: [[10, 12]]}, "SellTests/testBoth": {VIEW: [[10, 12]]},
                                     "MoneyTests/testMoney": {MODEL: [[5, 6]]}})
        self.assertEqual(picked({VIEW: modified(28, 1, ["    rare()"], ["    rarer()"])}, only_sell), (None, set()))

    def test_a_deleted_source_selects_every_test_that_ran_it(self):
        self.assertEqual(picked({MODEL: {"kind": "deleted", "hunks": []}}),
                         (None, {"SellTests/testBoth", "MoneyTests/testMoney"}))

    def test_a_new_source_selects_nothing_by_itself(self):
        self.assertEqual(picked({"App/Sources/Screens/NewView.swift": {"kind": "added", "hunks": []}}),
                         (None, set()))

    def test_a_source_the_map_has_no_line_for_selects_every_test(self):
        every, tests = picked({"Kit/Sources/Domain/Card.swift": modified(4, 1, ["  case a"], ["  case b"])})
        self.assertIn("no line in the test map", every)
        self.assertEqual(tests, EVERY)

    def test_a_ui_test_file_selects_its_tests_and_a_helper_the_tests_of_the_files_that_use_it(self):
        self.assertEqual(picked({"App/UITests/MoneyTests.swift": modified(9, 1, ["a"], ["b"])}),
                         (None, {"MoneyTests/testMoney"}))
        self.assertEqual(picked({"App/UITests/E2E.swift": modified(9, 1, ["a"], ["b"])}),
                         (None, {"SellTests/testSell", "SellTests/testBoth"}))
        self.assertEqual(picked({"App/UITests/StandInServer.swift": modified(9, 1, ["a"], ["b"])}), (None, set()))

    def test_a_helper_reaches_the_tests_of_a_file_through_another_helper(self):
        files = dict(FILES, **{
            "Waits.swift": {"classes": [], "tests": {}, "names": {"poll"}, "words": {"poll"}},
            "E2E.swift": dict(FILES["E2E.swift"], words={"E2ETestCase", "poll"})})
        selection = impact.select({"App/UITests/Waits.swift": modified(9, 1, ["a"], ["b"])}, MAP, CLASS_TESTS,
                                  files, PARENTS)
        self.assertEqual(set(selection["tests"]), {"SellTests/testSell", "SellTests/testBoth"})

    def test_the_file_the_stand_in_server_reads_selects_the_tests_that_use_the_server(self):
        files = dict(FILES, **{"SharingTests.swift": {"classes": ["SharingTests"], "tests": {"SharingTests": ["testShare"]},
                                                      "names": {"SharingTests"}, "words": {"SharingTests", "StandInServer"}}})
        class_tests = dict(CLASS_TESTS, SharingTests=["testShare"])
        test_map = dict(MAP, tests=dict(MAP["tests"], **{"SharingTests/testShare": {MODEL: [[5, 5]]}}))
        selection = impact.select({"api/openapi.json": modified(9, 1, ["a"], ["b"])}, test_map,
                                  class_tests, files, PARENTS)
        self.assertEqual((selection["all"], set(selection["tests"])), (None, {"SharingTests/testShare"}))

    def test_the_principal_class_of_the_test_bundle_selects_every_test(self):
        every, tests = picked({"App/UITests/TestImpact.swift": modified(9, 1, ["a"], ["b"])})
        self.assertIn("every test runs with it", every)
        self.assertEqual(tests, EVERY)

    def test_the_names_another_file_can_use(self):
        text = ("import XCTest\n"
                "let settle: TimeInterval = 0.5\n"
                "private let secret = 1\n"
                "func poll(timeout: TimeInterval) -> Bool {\n    let deadline = Date()\n    return true\n}\n"
                "@MainActor\nclass SellE2ECase: E2ETestCase {\n    func openCard() {\n        let panel = 1\n    }\n"
                "    private func hidden() {}\n}\n"
                "extension XCUIElement {\n    func appears(within timeout: TimeInterval) -> Bool {\n        let start = Date()\n"
                "        return true\n    }\n    var isShown: Bool { exists }\n    private func own() {}\n}\n"
                "final class SellTests: SellE2ECase {\n    func testSell() {}\n}\n")
        self.assertEqual(impact.exported_names(text), {"settle", "poll", "SellE2ECase", "appears", "isShown", "SellTests"})

    def test_a_name_behind_an_attribute_can_be_used_by_another_file_too(self):
        text = ("@available(iOS 17.0, *) func waitForSheet() {}\n"
                "@MainActor extension XCUIApplication {\n    func openSell() {\n        let row = 1\n    }\n}\n"
                "@MainActor\npublic final class Base<T>: XCTestCase {\n    func helper() {}\n}\n")
        self.assertEqual(impact.exported_names(text), {"waitForSheet", "openSell", "Base"})

    def test_a_comment_at_the_left_margin_declares_no_name(self):
        text = "/// The base class of the tests.\n// let old = 1\nclass Base: XCTestCase {}\n"
        self.assertEqual(impact.exported_names(text), {"Base"})

    def test_the_ui_test_files_of_the_example_know_who_uses_them(self):
        files, parents = impact.test_files(HERE / "Example" / "UITests")
        using = impact.users(files)
        self.assertEqual(using["Helpers.swift"], {"CounterTests.swift", "GreetingTests.swift"})
        self.assertEqual(using["CounterTests.swift"], set())
        self.assertEqual(files["GreetingTests.swift"]["tests"], {"GreetingTests": ["testGreetingShowsTheName"]})
        self.assertEqual(files["TestImpact.swift"]["tests"], {})
        self.assertEqual(parents["CounterTests"], "ExampleTestCase")

    def test_a_ui_test_file_with_a_base_class_selects_the_tests_of_its_subclasses(self):
        files = dict(FILES, **{"Base.swift": {"classes": ["SellBase"], "tests": {"SellBase": ["testShared"]},
                                              "names": {"SellBase"}, "words": {"SellBase", "E2ETestCase"}}})
        parents = dict(PARENTS, SellTests="SellBase", SellBase="E2ETestCase")
        class_tests = dict(CLASS_TESTS, SellBase=["testShared"])
        selection = impact.select({"App/UITests/Base.swift": modified(9, 1, ["a"], ["b"])}, MAP, class_tests,
                                  files, parents)
        self.assertEqual(set(selection["tests"]), {"SellBase/testShared", "SellTests/testSell", "SellTests/testBoth"})

    def test_a_deleted_ui_test_file_selects_nothing(self):
        self.assertEqual(picked({"App/UITests/OldTests.swift": {"kind": "deleted", "hunks": []}}), (None, set()))

    def test_documents_tool_output_and_other_programs_select_nothing(self):
        changes = {"docs/TESTING.md": modified(1, 1, ["a"], ["b"]), "App/README.md": modified(1, 1, ["a"], ["b"]),
                   "Kit/.swiftpm/xcode/x.xcscheme": {"kind": "added", "hunks": []},
                   "Kit/Tests/DomainTests/MoneyTests.swift": modified(1, 1, ["a"], ["b"]),
                   "tools/fetch.py": modified(1, 1, ["a"], ["b"]), "server/src/main.ts": modified(1, 1, ["a"], ["b"])}
        self.assertEqual(picked(changes), (None, set()))

    def test_a_file_the_build_reads_that_is_not_swift_selects_every_test(self):
        for path in ("App/project.yml", "App/Resources/Info.plist", "Kit/Package.swift"):
            every, tests = picked({path: modified(1, 1, ["a"], ["b"])})
            self.assertTrue(every, path)
            self.assertEqual(tests, EVERY, path)

    def test_a_file_outside_the_scope_selects_no_test(self):
        self.assertEqual(picked({"scripts/build.sh": modified(1, 1, ["a"], ["b"]),
                                 "impact.py": modified(1, 1, ["a"], ["b"])}), (None, set()))

    def test_a_folder_of_the_config_does_not_match_a_longer_name(self):
        every, _ = picked({"App/SourcesOld/A.swift": modified(1, 1, ["a"], ["b"])})
        self.assertIn("not Swift source", every)

    def test_an_unknown_key_of_the_config_is_an_error(self):
        with self.assertRaises(ValueError):
            impact.configure({"source": ["App/"]})

    def test_a_ui_test_folder_with_no_test_is_an_error_not_a_selection_of_nothing(self):
        with self.assertRaises(ValueError):
            impact.tree_tests(Path(tempfile.mkdtemp()))

    def test_the_passed_tests_of_a_result_bundle_have_their_class(self):
        results = {"testNodes": [{"nodeType": "Test Plan", "name": "P", "children": [
            {"nodeType": "UI test bundle", "name": "AppUITests", "children": [
                {"nodeType": "Test Suite", "name": "SellTests", "children": [
                    {"nodeType": "Test Case", "name": "testSell()", "result": "Passed"},
                    {"nodeType": "Test Case", "name": "testBoth()", "result": "Failed"}]}]}]}]}
        self.assertEqual(impact.passed_names(results), {"SellTests/testSell"})

    def test_a_test_the_map_does_not_have_is_always_selected_and_a_test_that_is_gone_never(self):
        class_tests = {"SellTests": ["testSell", "testNew"]}
        selection = impact.select({MODEL: modified(5, 1, ["a"], ["b"])}, MAP, class_tests, FILES, PARENTS)
        self.assertEqual(selection["tests"], {"SellTests/testNew": ["not in the test map"]})

    def test_each_selected_test_has_its_reasons(self):
        selection = impact.select({VIEW: modified(25, 1, ["    a()"], ["    b()"]),
                                   MODEL: modified(8, 1, ["    c()"], ["    d()"])}, MAP, CLASS_TESTS, FILES, PARENTS)
        self.assertEqual(selection["tests"], {"SellTests/testBoth": [f"{VIEW}: the tests that ran a changed line",
                                                                     f"{MODEL}: the tests that ran a changed line"]})


class Maps(unittest.TestCase):
    def test_line_numbers_become_ranges(self):
        self.assertEqual(impact.to_ranges({3, 1, 2, 7, 9, 8}), [[1, 3], [7, 9]])
        self.assertEqual(impact.to_ranges([]), [])

    def test_lcov_gives_the_count_of_each_line_of_the_files_under_the_root(self):
        text = ("SF:/work/ios/A.swift\nFN:1,f\nDA:1,2\nDA:2,0\nLF:2\nend_of_record\n"
                "SF:/other/B.swift\nDA:1,5\nend_of_record\nSF:/work/ios/A.swift\nDA:1,1\nend_of_record\n")
        self.assertEqual(impact.lcov_lines(text, "/work"), {"ios/A.swift": {1: 3, 2: 0}})

    def test_a_path_through_a_symbolic_link_is_under_the_root(self):
        real = Path(tempfile.mkdtemp()).resolve()
        link = real.parent / (real.name + "-link")
        link.symlink_to(real)
        self.assertEqual(impact.lcov_lines(f"SF:{link}/A.swift\nDA:1,1\nend_of_record\n", real), {"A.swift": {1: 1}})

    def test_coverage_files_are_grouped_by_the_test_in_their_name(self):
        folder = Path(tempfile.mkdtemp())
        for name in ("SellTests.testSell.101.profraw", "SellTests.testSell.102.profraw", "MoneyTests.testMoney.103.profraw",
                     "none.none.104.profraw", "other.txt"):
            (folder / name).write_text("")
        tests = impact.profile_tests([folder, folder / "missing"])
        self.assertEqual({t: [f.name for f in fs] for t, fs in tests.items()},
                         {"MoneyTests/testMoney": ["MoneyTests.testMoney.103.profraw"],
                          "SellTests/testSell": ["SellTests.testSell.101.profraw", "SellTests.testSell.102.profraw"]})

    def test_a_map_is_read_back_and_only_the_newest_are_kept(self):
        folder = Path(tempfile.mkdtemp())
        for n in range(impact.MAPS_KEEP + 2):
            path = impact.save_map(dict(MAP, commit=f"c{n}"), folder)
            subprocess.run(["touch", "-t", f"20261110120{n}", str(path)], check=True)
        found = impact.maps(folder)
        self.assertEqual([m["commit"] for m in found][0], f"c{impact.MAPS_KEEP + 1}")
        self.assertLessEqual(len(found), impact.MAPS_KEEP + 1)
        self.assertEqual(found[0]["tests"], MAP["tests"])

    def test_a_file_that_is_not_a_map_of_this_format_is_no_map(self):
        folder = Path(tempfile.mkdtemp())
        (folder / "bad.json.gz").write_text("not gzip")
        impact.save_map(dict(MAP, commit="old", format=0), folder)
        self.assertEqual(impact.maps(folder), [])

    def test_the_map_nearest_to_the_tree_is_used_not_the_one_that_selects_the_fewest_tests(self):
        root, folder = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())

        def git(*args):
            return subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                                  check=True, capture_output=True, text=True).stdout.strip()
        source = root / VIEW
        source.parent.mkdir(parents=True)
        git("init", "-q")
        commits = []
        for text in ("\n".join(f"line {n}" for n in range(1, 31)) + "\n", "\n".join(f"LINE {n}" for n in range(1, 31)) + "\n"):
            source.write_text(text)
            git("add", ".")
            git("commit", "-qm", "c")
            commits.append(git("rev-parse", "HEAD"))
        far = dict(MAP, commit=commits[0], tests={"SellTests/testSell": {MODEL: [[5, 6]]},
                                                  "SellTests/testBoth": {MODEL: [[5, 6]]}, "MoneyTests/testMoney": {MODEL: [[5, 6]]}})
        near = dict(MAP, commit=commits[1])
        for test_map, minute in ((near, 1), (far, 2)):
            subprocess.run(["touch", "-t", f"20261110120{minute}", str(impact.save_map(test_map, folder))], check=True)
        source.write_text(source.read_text().replace("LINE 25\n", "LINE twenty-five\n"))
        selection, used = impact.choose(CLASS_TESTS, FILES, PARENTS, root=root, folder=folder)
        self.assertEqual(used["commit"], commits[1])
        self.assertEqual((set(selection["tests"]), selection["changed"]), ({"SellTests/testBoth"}, [VIEW]))

    def test_a_generic_base_class_has_its_subclasses(self):
        folder = Path(tempfile.mkdtemp())
        (folder / "A.swift").write_text("class Base<T>: XCTestCase {\n}\nclass Sub: Base<Int> {\n  func testOne() {}\n}\n")
        files, parents = impact.test_files(folder)
        self.assertEqual(parents, {"Base": "XCTestCase", "Sub": "Base"})
        self.assertEqual(impact.with_subclasses(["Base"], parents), {"Base", "Sub"})

    def test_with_no_map_the_answer_says_why(self):
        selection, why = impact.choose(CLASS_TESTS, FILES, PARENTS, root=HERE, folder=Path(tempfile.mkdtemp()))
        self.assertIsNone(selection)
        self.assertIn("no test map", why)

    def test_a_map_of_a_commit_git_does_not_know_is_not_used(self):
        folder = Path(tempfile.mkdtemp())
        impact.save_map(dict(MAP, commit="0" * 40), folder)
        selection, why = impact.choose(CLASS_TESTS, FILES, PARENTS, root=HERE, folder=folder)
        self.assertIsNone(selection)
        self.assertIn("not of a commit in this repository", why)


if __name__ == "__main__":
    unittest.main()
