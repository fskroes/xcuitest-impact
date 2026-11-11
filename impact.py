#!/usr/bin/env python3
"""impact: which XCUITest UI tests a change can reach, from the lines of the app that each test ran.

A run of every UI test on a build with coverage counters writes a test map: for each UI test, the lines of the app
that it ran (TestImpact.swift names one coverage file per app launch). Later, `plan` compares the work tree with the
commit of a map and gives the tests that ran a changed line. Run from inside the git repository of the app:

    impact.py record --profiles DIR --app PATH/Your.app --xcresult PATH   write the map of the HEAD commit
    impact.py plan                the tests the changes of this tree select, with the reason for each (JSON)
    impact.py plan --args         the same as -only-testing arguments for xcodebuild
    impact.py maps                the maps on this Mac

`plan --args` prints one argument per line. Exit 0 with no line: run every test. Exit 3: no test is needed.
Exit 2: the config is wrong.

The rules are in `select`. They err to more tests: a change that the map cannot place selects every test.
A map belongs to a commit, and a tree is compared with that commit, so a map stays usable after the branch moves;
the selection only grows with the distance.

The project is described in `test-impact.json` at the root of the repository (see DEFAULTS).
"""
import argparse
import gzip
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

CONFIG_FILE = "test-impact.json"
DEFAULTS = {
    # Folders whose Swift files are compiled into the app (the app target, local packages), so the map has their lines.
    "sources": ["App/Sources/"],
    # The folder of the UI test target.
    "ui_tests": "App/UITests/",
    # The name of the UI test target, for -only-testing.
    "target": "AppUITests",
    # Only a change under one of these can reach a UI test. A file here that is not Swift source (the project file,
    # a resource, Package.swift) selects every test.
    "scope": ["App/"],
    # Folders under "scope" whose change reaches no UI test (unit tests of a package).
    "ignore": [],
    # A file outside the UI test folder that one UI test file reads: a change to it counts as a change to that file.
    "read_by": {},
}


def repo_root():
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return Path(top.stdout.strip()) if top.returncode == 0 and top.stdout.strip() else Path.cwd()


def repo_name(root):
    """The folder name of the main checkout, so every git worktree of a repository uses the same maps."""
    common = subprocess.run(["git", "-C", str(root), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                            capture_output=True, text=True).stdout.strip()
    return Path(common).parent.name if common.endswith("/.git") else Path(root).name


ROOT = repo_root()
MAPS = Path(os.environ.get("TEST_IMPACT_MAPS") or Path.home() / ".cache" / "test-impact" / repo_name(ROOT))
MAPS_KEEP = 4          # maps on disk; `choose` reads the newest MAPS_TRIED of them
MAPS_TRIED = 3
FORMAT = 1
# UI test files that every test runs with, whatever names it uses: TestImpact is the principal class of the bundle.
EVERY_TEST_HELPERS = ("TestImpact.swift",)
SOURCES, UITESTS, TARGET, SCOPE, NO_TEST_PREFIXES, READ_BY = (), "", "", (), (), {}


def configure(config=None):
    """Sets the description of the project: DEFAULTS, with the keys of `config` in their place. A folder gets its
    closing slash, so "App" does not also match "AppClip/"."""
    global SOURCES, UITESTS, TARGET, SCOPE, NO_TEST_PREFIXES, READ_BY
    unknown = set(config or {}) - set(DEFAULTS)
    if unknown:
        raise ValueError(f"{CONFIG_FILE}: unknown key {sorted(unknown)[0]}")
    cfg = {**DEFAULTS, **(config or {})}

    def folder(path):
        return path if path.endswith("/") else path + "/"
    SOURCES = tuple(map(folder, cfg["sources"]))
    UITESTS, TARGET = folder(cfg["ui_tests"]), cfg["target"]
    SCOPE, NO_TEST_PREFIXES = tuple(map(folder, cfg["scope"])), tuple(map(folder, cfg["ignore"]))
    READ_BY = dict(cfg["read_by"])


def load_config(root=ROOT):
    path = Path(root) / CONFIG_FILE
    configure(json.loads(path.read_text()) if path.is_file() else None)


configure()
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
DIFF_HEADER = re.compile(r"^diff --git a/(.*) b/(.*)$")
NOT_CODE = re.compile(r"^\s*(//.*)?$")
CLASS_WITH_PARENT = re.compile(r"\bclass\s+(\w+)\s*(?:<[^>{]*>)?\s*:\s*([\w.]+)")
TEST_FUNC = re.compile(r"\bfunc\s+(test\w+)\s*\(\s*\)")   # a test takes no argument (testCaseWillStart(_:) is none)


# ---- the changes of a tree against a commit

def parse_diff(text):
    """{path: {"kind": "added" | "deleted" | "modified", "hunks": [hunk]}} of `git diff --no-renames -U0`. A hunk is
    {"old": first old line, "count": old lines, "removed": [text], "added": [text]}; with count 0 the lines were put
    in after line "old". A binary file has no hunks."""
    changes, current, hunk = {}, None, None
    for line in text.splitlines():
        header = DIFF_HEADER.match(line)
        if header:
            current = changes.setdefault(header.group(2), {"kind": "modified", "hunks": []})
            hunk = None
        elif current is None:
            continue
        elif line.startswith("new file mode"):
            current["kind"] = "added"
        elif line.startswith("deleted file mode"):
            current["kind"] = "deleted"
        elif line.startswith("@@"):
            m = HUNK.match(line)
            if m:
                hunk = {"old": int(m.group(1)), "count": int(m.group(2) if m.group(2) is not None else 1),
                        "removed": [], "added": []}
                current["hunks"].append(hunk)
        elif hunk is not None and line.startswith("-") and not line.startswith("---"):
            hunk["removed"].append(line[1:])
        elif hunk is not None and line.startswith("+") and not line.startswith("+++"):
            hunk["added"].append(line[1:])
    return changes


def tree_changes(commit, root=ROOT):
    """parse_diff of the work tree of `root` against `commit`, with the files git does not track yet as added. None
    when git cannot compare them (the commit is not in this repository)."""
    def git(*args):
        return subprocess.run(["git", "-C", str(root), "-c", "core.quotePath=false", *args], capture_output=True,
                              text=True, errors="replace")
    diff = git("diff", "--no-renames", "--no-ext-diff", "--no-color", "-U0", commit, "--")
    if diff.returncode != 0:
        return None
    changes = parse_diff(diff.stdout)
    for path in git("ls-files", "-o", "--exclude-standard").stdout.splitlines():
        if path:
            changes.setdefault(path, {"kind": "added", "hunks": []})
    return changes


# ---- the tests of the UI test folder

# A declaration that starts at the left margin (not a comment), after any attributes and modifiers on its line
# ("@available(iOS 17.0, *) func waitForSheet()", "@MainActor final class SellE2ECase").
TOP_LEVEL_NAME = re.compile(r"^(?![ \t/])(?!.*\b(?:private|fileprivate)\b).*?\b(?:class|struct|enum|protocol|actor|"
                            r"typealias|func|let|var)\s+(\w+)", re.M)
EXTENSION = re.compile(r"^(?![ \t/])[^\n{]*?\bextension\s+[\w.]+[^{\n]*\{", re.M)
MEMBER_NAME = re.compile(r"^(?!.*\b(?:private|fileprivate)\b).*?\b(?:func|var|let)\s+(\w+)", re.M)
WORD = re.compile(r"\w+")


def exported_names(text):
    """The names of a Swift file that another file of the same target can use: what it declares at the left margin
    (types, functions, constants) and the members of its extensions, without the private ones. A member of a class
    is not among them: another file reaches it only through the name of the class or of a subclass."""
    names = {m.group(1) for m in TOP_LEVEL_NAME.finditer(text)}
    for m in EXTENSION.finditer(text):
        depth, start, members = 1, m.end(), []
        for end in range(m.end(), len(text)):   # the text of the extension that is not inside a body of its own
            step = {"{": 1, "}": -1}.get(text[end], 0)
            if step == 1 and depth == 1:
                members.append(text[start:end])
            depth += step
            if step == -1 and depth == 1:
                start = end + 1
            if depth == 0:
                members.append(text[start:end])
                break
        names.update(MEMBER_NAME.findall("\n".join(members)))
    return names


def users(files):
    """{file: the files that use one of its exported names, or use a file that does} for test_files entries. A
    file uses a name when the name is a word of its text; that also counts a word that only looks the same, so it
    errs to more files."""
    direct = {name: {other for other, entry in files.items()
                     if other != name and files[name]["names"] & entry["words"]} for name in files}
    for name in files:
        found, grew = set(direct[name]), True
        while grew:
            more = {u for f in found for u in direct[f]} - found - {name}
            found, grew = found | more, bool(more)
        direct[name] = found
    return direct


def test_files(uitests_dir, strip=lambda text: text):
    """{file name: {"classes": [classes declared], "tests": {class: [tests]}, "names": exported_names, "words": the
    words of its text}} and {class: parent} for the Swift files of the UI test folder. A test belongs to the last
    class declared above it in its file."""
    files, parents = {}, {}
    for path in sorted(Path(uitests_dir).rglob("*.swift")):
        text = strip(path.read_text())
        decls = [(m.start(), m.group(1)) for m in CLASS_WITH_PARENT.finditer(text)]
        parents.update({m.group(1): m.group(2) for m in CLASS_WITH_PARENT.finditer(text)})
        tests = {}
        for m in TEST_FUNC.finditer(text):
            owner = [name for start, name in decls if start < m.start()]
            if owner:
                tests.setdefault(owner[-1], []).append(m.group(1))
        files[path.name] = {"classes": [name for _, name in decls], "tests": tests,
                            "names": exported_names(text), "words": set(WORD.findall(text))}
    return files, parents


def with_subclasses(classes, parents):
    """`classes` and every class that has one of them as an ancestor."""
    found = set(classes)
    grew = True
    while grew:
        grew = False
        for child, parent in parents.items():
            if parent in found and child not in found:
                found.add(child)
                grew = True
    return found


# ---- the selection

def in_ranges(ranges, line):
    return any(a <= line <= b for a, b in ranges)


def any_in_ranges(ranges, lines):
    return any(in_ranges(ranges, line) for line in lines)


def is_code(line):
    return not NOT_CODE.match(line)


def hidden(path):
    """A path with a part that starts with a dot (.swiftpm, .build, .DS_Store): tool output, never a source."""
    return any(part.startswith(".") for part in path.split("/"))


def relevant(path):
    """True for a file whose change can reach a UI test: a file under a "scope" folder or one of "read_by", and not
    a document, tool output or a file under an "ignore" folder."""
    if path.endswith(".md") or hidden(path) or path.startswith(NO_TEST_PREFIXES):
        return False
    return path in READ_BY or path.startswith(SCOPE)


def uncommitted(root=ROOT):
    """The relevant files of the tree that differ from its HEAD commit, or None when git cannot tell. A test map is
    of a commit, so only a run on a tree with none of these can write one."""
    changes = tree_changes("HEAD", root)
    return None if changes is None else sorted(filter(relevant, changes))


def file_selection(path, change, test_map):
    """What a change to one Swift source of the app selects: (tests, reason), or ("all", reason) when the map cannot
    place the change. `tests` are "Class/test" names."""
    lines = test_map["lines"].get(path)
    ran = {t: files[path] for t, files in test_map["tests"].items() if path in files}
    if change["kind"] == "added":
        return set(), None   # no test ran it; the lines that call it are changes of their own
    if lines is None:
        # The file was in the build of the map and has no line that can run: declarations only (stored properties,
        # cases, a protocol). No test can be tied to it by a line, and its change can still change what runs.
        return "all", f"{path} has no line in the test map"
    if change["kind"] == "deleted" or not change["hunks"]:
        return set(ran), f"{path} is gone or changed as a whole: every test that ran it"
    picked, whole_file = set(), False
    for hunk in change["hunks"]:
        if not any(map(is_code, hunk["removed"] + hunk["added"])):
            continue   # blank lines and // comments
        if hunk["count"]:
            first, last = hunk["old"], hunk["old"] + hunk["count"] - 1
            code = [first + n for n, text in enumerate(hunk["removed"]) if is_code(text)]
            # A changed line of code that cannot run is a declaration: which tests it reaches is not in the lines.
            placed = all(in_ranges(lines, line) for line in code)
        else:
            first, last = hunk["old"] + 1, hunk["old"]   # nothing removed: the lines went in between old and old + 1
            # Inside a body both sides can run. With one side that cannot, the new lines can be a declaration (a
            # stored property with a default that does something, a method that takes the place of a default
            # one): every test that makes the type runs it, not only the tests of the lines next to it.
            placed = in_ranges(lines, hunk["old"]) and in_ranges(lines, hunk["old"] + 1)
        if not placed:
            whole_file = True
            break
        around = range(first - 1, last + 2)   # the changed lines and the line on each side
        picked |= {t for t, covered in ran.items() if any_in_ranges(covered, around)}
    if whole_file:
        return set(ran), f"{path}: a changed line is a declaration, so every test that ran a line of the file"
    return picked, f"{path}: the tests that ran a changed line"


def select(changes, test_map, class_tests, files, parents):
    """The UI tests a change selects: {"all": why or None, "tests": {"Class/test": [reasons]}}.

    class_tests is {class: [tests]} of the tree now; files and parents are test_files of the tree now.

    - A test the map does not have (new, or it gave no coverage when the map was made) is always selected.
    - A Swift file under "sources": file_selection.
    - A UI test file: its own tests, the tests of the subclasses of its classes, and the tests of every file that
      uses one of its exported names (`users`). A helper that few files use selects few tests; the file with the
      base class of most tests selects most. EVERY_TEST_HELPERS select every test.
    - A file of "read_by": as a change to the UI test file that reads it.
    - Any other file under "scope" that is not a document (the project file, a resource, Package.swift): every test.
    - Anything else (another program of the repository, a document, a build script): no UI test.
    """
    every = sorted(f"{c}/{t}" for c, ts in class_tests.items() for t in ts)
    reasons = {}

    def add(tests, why):
        for test in tests:
            if test in reasons or test in every_set:
                reasons.setdefault(test, []).append(why)

    every_set = set(every)
    add([t for t in every if t not in test_map["tests"]], "not in the test map")
    for path in sorted(changes):
        change = changes[path]
        if not relevant(path):
            continue
        if path in READ_BY or (path.startswith(UITESTS) and path.endswith(".swift")):
            name = READ_BY.get(path) or path[len(UITESTS):]
            entry = files.get(name)
            if name in EVERY_TEST_HELPERS:
                return {"all": f"{path} changed: every test runs with it", "tests": {t: [path] for t in every}}
            if entry is None:
                if change["kind"] == "deleted":
                    continue   # its tests are gone with it; a helper that is gone no longer compiles into the others
                return {"all": f"{path} is not a file of the UI tests", "tests": {t: [path] for t in every}}
            using = users(files)[name]
            classes = with_subclasses(entry["classes"], parents) | {c for f in using for c in files[f]["tests"]}
            add([f"{c}/{t}" for c in sorted(classes) for t in class_tests.get(c, [])],
                f"{path} changed: its tests and the tests of the files that use it")
            continue
        if path.startswith(SOURCES) and path.endswith(".swift"):
            tests, why = file_selection(path, change, test_map)
            if tests == "all":
                return {"all": why, "tests": {t: [why] for t in every}}
            add(tests, why)
            continue
        return {"all": f"{path} is not Swift source: the map has no lines for it",
                "tests": {t: [path] for t in every}}
    return {"all": None, "tests": dict(sorted(reasons.items()))}


# ---- the maps on disk

def map_path(commit, folder=MAPS):
    return Path(folder) / f"{commit}.json.gz"


def save_map(test_map, folder=MAPS):
    """Writes the map of its commit and removes all but the newest MAPS_KEEP."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = map_path(test_map["commit"], folder)
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wt") as f:
        json.dump(test_map, f, separators=(",", ":"))
    tmp.replace(path)
    for old in sorted(folder.glob("*.json.gz"), key=lambda p: p.stat().st_mtime, reverse=True)[MAPS_KEEP:]:
        old.unlink(missing_ok=True)
    return path


def load_map(path):
    try:
        with gzip.open(path, "rt") as f:
            data = json.load(f)
    except (OSError, ValueError, EOFError):
        return None
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        return None
    return data


def maps(folder=MAPS):
    """The maps on disk, newest first."""
    paths = sorted(Path(folder).glob("*.json.gz"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [m for m in map(load_map, paths) if m]


def choose(class_tests, files, parents, root=ROOT, folder=MAPS):
    """(selection, map) of the map nearest to this tree among the newest maps that git can compare with it, or
    (None, why) when there is none. Nearest is the fewest changed lines in the files that count (`relevant`), then
    the newest. It is not the map that selects the fewest tests: that would choose by the answer, and a test that
    one map ties to a line and another does not would then never run. A selection is the result of `select` with
    "commit", the commit of its map, and "changed", the files that count."""
    best, distance = None, None
    for test_map in maps(folder)[:MAPS_TRIED]:
        changes = tree_changes(test_map["commit"], root)
        if changes is None:
            continue
        changed = sorted(filter(relevant, changes))
        far = sum(1 + sum(len(h["removed"]) + len(h["added"]) for h in changes[path]["hunks"]) for path in changed)
        if best is None or far < distance:
            selection = select(changes, test_map, class_tests, files, parents)
            selection["commit"], selection["changed"] = test_map["commit"], changed
            best, distance = (selection, test_map), far
    if best is None:
        return None, (f"the test maps in {folder} are not of a commit in this repository" if maps(folder)
                      else f"no test map in {folder}")
    return best


# ---- making a map from the coverage files of a run

def lcov_lines(text, root):
    """{path under root: {line: count}} of `llvm-cov export -format=lcov`. A file outside root is left out. Both
    sides are compared without symbolic links: the compiler has the path the build was started with (/tmp/x), git
    gives the real one (/private/tmp/x)."""
    prefix = os.path.realpath(root).rstrip("/") + "/"
    files, current = {}, None
    for line in text.splitlines():
        if line.startswith("SF:"):
            path = os.path.realpath(line[3:])
            current = files.setdefault(path[len(prefix):], {}) if path.startswith(prefix) else None
        elif line.startswith("DA:") and current is not None:
            number, _, count = line[3:].partition(",")
            try:
                current[int(number)] = current.get(int(number), 0) + int(count.split(",")[0])
            except ValueError:
                continue
    return files


def to_ranges(lines):
    """Sorted line numbers as [[first, last], ...]."""
    ranges = []
    for line in sorted(lines):
        if ranges and line == ranges[-1][1] + 1:
            ranges[-1][1] = line
        else:
            ranges.append([line, line])
    return ranges


def profile_tests(folders):
    """{"Class/test": [its coverage files]} of the files TestImpact.swift names "<Class>.<test>.<pid>.profraw", in
    every folder of `folders` (one per machine, when the tests ran on more than one)."""
    tests = {}
    for folder in folders:
        for path in sorted(Path(folder).glob("*.profraw")):
            parts = path.name.split(".")
            if len(parts) == 4 and parts[0] != "none":
                tests.setdefault(f"{parts[0]}/{parts[1]}", []).append(path)
    return tests


def coverage_objects(app):
    """The binaries of the built app that can hold coverage: its executable, and every dylib and framework in it (a
    debug build keeps the code in <App>.debug.dylib)."""
    app = Path(app)
    found = [app / app.stem] + sorted(app.glob("*.dylib"))
    found += sorted(p / p.stem for p in (app / "Frameworks").glob("*.framework"))
    return [p for p in found if p.is_file()]


def test_coverage(files, objects, root, work):
    """({path: {line: count}}, error) of one test: its coverage files merged, then exported per line."""
    profdata = Path(work) / (files[0].name.rsplit(".", 2)[0] + ".profdata")
    merge = subprocess.run(["xcrun", "llvm-profdata", "merge", "-sparse", *map(str, files), "-o", str(profdata)],
                           capture_output=True, text=True)
    if merge.returncode != 0:
        return None, (merge.stderr.strip().splitlines() or ["llvm-profdata failed"])[-1]
    args = [str(objects[0])] + [a for o in objects[1:] for a in ("-object", str(o))]
    export = subprocess.run(["xcrun", "llvm-cov", "export", "-format=lcov", f"-instr-profile={profdata}", *args],
                            capture_output=True, text=True)
    profdata.unlink(missing_ok=True)
    if export.returncode != 0:
        return None, (export.stderr.strip().splitlines() or ["llvm-cov failed"])[-1]
    return lcov_lines(export.stdout, root), None


def build_map(profiles, app, commit, passed, root=ROOT, jobs=4):
    """The test map of a run: profiles are the folders of its coverage files, app the built .app, passed the
    "Class/test" names that passed. Only a test that passed gets lines: a failed test stopped early, so its lines
    are not all the lines it reaches, and it stays out of the map, which selects it every time.

    {"format", "commit", "when", "lines": {path: ranges of the lines that can run},
     "tests": {"Class/test": {path: ranges of the lines it ran}}, "problems": {"Class/test": why it has no lines}}"""
    objects = coverage_objects(app)
    by_test = {t: fs for t, fs in profile_tests(profiles).items() if t in passed}
    lines, tests, problems = {}, {}, {}
    if not objects:
        return {"format": FORMAT, "commit": commit, "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "lines": {},
                "tests": {}, "problems": {t: f"no binary in {app}" for t in by_test}}
    with tempfile.TemporaryDirectory() as work, ThreadPoolExecutor(jobs) as pool:
        results = pool.map(lambda item: (item[0], test_coverage(item[1], objects, root, work)), by_test.items())
        for test, (coverage, error) in results:
            if coverage is None:
                problems[test] = error
                continue
            ran = {}
            for path, counts in coverage.items():
                lines.setdefault(path, set()).update(counts)
                hit = [line for line, count in counts.items() if count > 0]
                if hit:
                    ran[path] = to_ranges(hit)
            if ran:
                tests[test] = ran
            else:
                problems[test] = "its coverage files have no line that ran"
    for test in sorted(set(passed) - set(by_test)):
        problems[test] = "no coverage file"
    return {"format": FORMAT, "commit": commit, "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "lines": {p: to_ranges(ls) for p, ls in sorted(lines.items())}, "tests": dict(sorted(tests.items())),
            "problems": dict(sorted(problems.items()))}


# ---- the command line

def passed_tests(xcresult):
    """The "Class/test" names that passed in an .xcresult, or None when xcresulttool cannot read it. A test that
    passed on a retry counts: its last run reached all its lines."""
    p = subprocess.run(["xcrun", "xcresulttool", "get", "test-results", "tests", "--path", str(xcresult)],
                       capture_output=True, text=True)
    try:
        data = json.loads(p.stdout) if p.returncode == 0 else None
    except ValueError:
        data = None
    return None if data is None else passed_names(data)


def passed_names(results):
    """The "Class/test" names with result Passed in the JSON of `xcresulttool get test-results tests`. The class of
    a test is the Test Suite node above it."""
    passed = set()

    def walk(node, suite):
        kind, name = node.get("nodeType"), node.get("name", "")
        if kind == "Test Case" and suite and node.get("result") == "Passed":
            passed.add(f"{suite}/{name.removesuffix('()')}")
        for child in node.get("children", []):
            walk(child, name if kind == "Test Suite" else suite)

    for node in results.get("testNodes", []):
        walk(node, None)
    return passed


def record(args):
    """Writes the map of the HEAD commit from the coverage files of a run of every UI test."""
    dirty = uncommitted()
    head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    if dirty is None or not head:
        print("impact: this folder is not in a git repository with a commit", file=sys.stderr)
        return 1
    if dirty:
        print(f"impact: no map: a map is of a commit, and the tree has changes ({', '.join(dirty[:5])})",
              file=sys.stderr)
        return 1
    wanted = sum(map(len, tree_tests()[0].values()))
    passed = passed_tests(args.xcresult)
    if passed is None:
        print(f"impact: cannot read the test results of {args.xcresult}", file=sys.stderr)
        return 1
    test_map = build_map(args.profiles, args.app, head, passed)
    print(json.dumps({"commit": head, "tests": len(test_map["tests"]), "of": wanted, "files": len(test_map["lines"]),
                      "problems": test_map["problems"]}, indent=1))
    if not test_map["tests"]:
        print("impact: no map: no test has coverage (see the README, \"When the map stays empty\")", file=sys.stderr)
        return 1
    print(f"impact: map written to {save_map(test_map)}", file=sys.stderr)
    return 0


def tree_tests(root=ROOT):
    """({class: [tests]}, files, parents) of the UI test folder now. A folder with no test is an error of the
    config, not a tree with nothing to run: a selection of zero tests would then look like "no test is needed"."""
    files, parents = test_files(Path(root) / UITESTS)
    class_tests = {}
    for entry in files.values():
        for c, ts in entry["tests"].items():
            class_tests.setdefault(c, []).extend(ts)
    if not class_tests:
        raise ValueError(f"no UI test found in {UITESTS} (\"ui_tests\" in {CONFIG_FILE})")
    return class_tests, files, parents


def main(argv):
    parser = argparse.ArgumentParser(prog="impact.py", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="the tests the changes of this tree select")
    plan.add_argument("--args", action="store_true", help="print -only-testing arguments, one per line")
    rec = commands.add_parser("record", help="write the test map of the HEAD commit")
    rec.add_argument("--profiles", action="append", required=True, help="folder of the .profraw files (repeatable)")
    rec.add_argument("--app", required=True, help="the built .app that the tests ran")
    rec.add_argument("--xcresult", required=True, help="the result bundle of the test run")
    commands.add_parser("maps", help="the maps on this Mac")
    args = parser.parse_args(argv)
    try:
        load_config()
        return run(args)
    except ValueError as error:   # the config: not JSON, an unknown key, a UI test folder with no test
        print(f"impact: {error}", file=sys.stderr)
        return 2


def run(args):
    if args.command == "maps":
        for m in maps():
            print(json.dumps({"commit": m["commit"], "when": m["when"], "tests": len(m["tests"]),
                              "files": len(m["lines"]), "problems": m["problems"]}))
        return 0
    if args.command == "record":
        return record(args)
    class_tests, files, parents = tree_tests()
    total = sum(map(len, class_tests.values()))
    selection, test_map = choose(class_tests, files, parents)
    if args.args:
        if selection is None or selection["all"]:
            print(f"impact: every test: {test_map if selection is None else selection['all']}", file=sys.stderr)
            return 0
        print(f"impact: {len(selection['tests'])} of {total} tests", file=sys.stderr)
        for test in selection["tests"]:
            print(f"-only-testing:{TARGET}/{test}")
        return 0 if selection["tests"] else 3
    if selection is None:
        print(json.dumps({"all": test_map, "tests": total}))
        return 0
    print(json.dumps({"map": selection["commit"], "all": selection["all"], "selected": len(selection["tests"]),
                      "of": total, "tests": selection["tests"]}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
