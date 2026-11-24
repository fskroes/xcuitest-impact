#!/usr/bin/env python3
USAGE = """impact: which XCUITest UI tests a change can reach, from the lines of the app that each test ran.

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
    "sources": ["App/Sources/"],
    "ui_tests": "App/UITests/",
    "target": "AppUITests",
    "scope": ["App/"],
    "ignore": [],
    "read_by": {},
}


def repo_root():
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return Path(top.stdout.strip()) if top.returncode == 0 and top.stdout.strip() else Path.cwd()


def repo_name(root):
    common = subprocess.run(["git", "-C", str(root), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                            capture_output=True, text=True).stdout.strip()
    return Path(common).parent.name if common.endswith("/.git") else Path(root).name


ROOT = repo_root()
MAPS = Path(os.environ.get("TEST_IMPACT_MAPS") or Path.home() / ".cache" / "test-impact" / repo_name(ROOT))
MAPS_KEEP = 4
MAPS_TRIED = 3
FORMAT = 1
EVERY_TEST_HELPERS = ("TestImpact.swift",)
SOURCES, UITESTS, TARGET, SCOPE, NO_TEST_PREFIXES, READ_BY = (), "", "", (), (), {}


def configure(config=None):
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
TEST_FUNC = re.compile(r"\bfunc\s+(test\w+)\s*\(\s*\)")


def parse_diff(text):
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


TOP_LEVEL_NAME = re.compile(r"^(?![ \t/])(?!.*\b(?:private|fileprivate)\b).*?\b(?:class|struct|enum|protocol|actor|"
                            r"typealias|func|let|var)\s+(\w+)", re.M)
EXTENSION = re.compile(r"^(?![ \t/])[^\n{]*?\bextension\s+[\w.]+[^{\n]*\{", re.M)
MEMBER_NAME = re.compile(r"^(?!.*\b(?:private|fileprivate)\b).*?\b(?:func|var|let)\s+(\w+)", re.M)
WORD = re.compile(r"\w+")


def exported_names(text):
    names = {m.group(1) for m in TOP_LEVEL_NAME.finditer(text)}
    for m in EXTENSION.finditer(text):
        depth, start, members = 1, m.end(), []
        for end in range(m.end(), len(text)):
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
    found = set(classes)
    grew = True
    while grew:
        grew = False
        for child, parent in parents.items():
            if parent in found and child not in found:
                found.add(child)
                grew = True
    return found


def in_ranges(ranges, line):
    return any(a <= line <= b for a, b in ranges)


def any_in_ranges(ranges, lines):
    return any(in_ranges(ranges, line) for line in lines)


def is_code(line):
    return not NOT_CODE.match(line)


def hidden(path):
    return any(part.startswith(".") for part in path.split("/"))


def relevant(path):
    if path.endswith(".md") or hidden(path) or path.startswith(NO_TEST_PREFIXES):
        return False
    return path in READ_BY or path.startswith(SCOPE)


def uncommitted(root=ROOT):
    changes = tree_changes("HEAD", root)
    return None if changes is None else sorted(filter(relevant, changes))


def file_selection(path, change, test_map):
    lines = test_map["lines"].get(path)
    ran = {t: files[path] for t, files in test_map["tests"].items() if path in files}
    if change["kind"] == "added":
        return set(), None
    if lines is None:
        return "all", f"{path} has no line in the test map"
    if change["kind"] == "deleted" or not change["hunks"]:
        return set(ran), f"{path} is gone or changed as a whole: every test that ran it"
    picked, whole_file = set(), False
    for hunk in change["hunks"]:
        if not any(map(is_code, hunk["removed"] + hunk["added"])):
            continue
        if hunk["count"]:
            first, last = hunk["old"], hunk["old"] + hunk["count"] - 1
            code = [first + n for n, text in enumerate(hunk["removed"]) if is_code(text)]
            placed = all(in_ranges(lines, line) for line in code)
        else:
            first, last = hunk["old"] + 1, hunk["old"]
            placed = in_ranges(lines, hunk["old"]) and in_ranges(lines, hunk["old"] + 1)
        if not placed:
            whole_file = True
            break
        around = range(first - 1, last + 2)
        picked |= {t for t, covered in ran.items() if any_in_ranges(covered, around)}
    if whole_file:
        return set(ran), f"{path}: a changed line is a declaration, so every test that ran a line of the file"
    return picked, f"{path}: the tests that ran a changed line"


def select(changes, test_map, class_tests, files, parents):
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
                    continue
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


def map_path(commit, folder=MAPS):
    return Path(folder) / f"{commit}.json.gz"


def save_map(test_map, folder=MAPS):
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
    paths = sorted(Path(folder).glob("*.json.gz"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [m for m in map(load_map, paths) if m]


def choose(class_tests, files, parents, root=ROOT, folder=MAPS):
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


def lcov_lines(text, root):
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
    ranges = []
    for line in sorted(lines):
        if ranges and line == ranges[-1][1] + 1:
            ranges[-1][1] = line
        else:
            ranges.append([line, line])
    return ranges


def profile_tests(folders):
    tests = {}
    for folder in folders:
        for path in sorted(Path(folder).glob("*.profraw")):
            parts = path.name.split(".")
            if len(parts) == 4 and parts[0] != "none":
                tests.setdefault(f"{parts[0]}/{parts[1]}", []).append(path)
    return tests


def coverage_objects(app):
    app = Path(app)
    found = [app / app.stem] + sorted(app.glob("*.dylib"))
    found += sorted(p / p.stem for p in (app / "Frameworks").glob("*.framework"))
    return [p for p in found if p.is_file()]


def test_coverage(files, objects, root, work):
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


def passed_tests(xcresult):
    p = subprocess.run(["xcrun", "xcresulttool", "get", "test-results", "tests", "--path", str(xcresult)],
                       capture_output=True, text=True)
    try:
        data = json.loads(p.stdout) if p.returncode == 0 else None
    except ValueError:
        data = None
    return None if data is None else passed_names(data)


def passed_names(results):
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
    files, parents = test_files(Path(root) / UITESTS)
    class_tests = {}
    for entry in files.values():
        for c, ts in entry["tests"].items():
            class_tests.setdefault(c, []).extend(ts)
    if not class_tests:
        raise ValueError(f"no UI test found in {UITESTS} (\"ui_tests\" in {CONFIG_FILE})")
    return class_tests, files, parents


def main(argv):
    parser = argparse.ArgumentParser(prog="impact.py", description=USAGE.split("\n\n")[0])
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
    except ValueError as error:
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
