# xcuitest-impact

Run only the XCUITest UI tests that a change can reach.

A coding agent that changes one line of an iOS app should not wait for every UI test. This repository has the method, the measurements, and the two files that do the work: `impact.py` (one Python file, no dependency) and `TestImpact.swift` (about 50 lines, goes into your UI test target).

## The result

Measured on 2026-11-10 on a private SwiftUI app with 175 UI tests. You cannot repeat these numbers from this repository; the example app here shows the mechanism, not the gain. One Mac, Apple M5, 10 cores, 16 GB, Xcode 27.2. The baseline is the median of the 20 full runs before the change: 1732 s (29 minutes).

| Run | Wall time | Against 1732 s |
|---|---|---|
| One changed line of one screen: 6 of 175 tests, on a simulator that was already booted | 139 s | 92% less |
| The same 6 tests on four simulators that had just booted | 410 s | 76% less |
| A change to a parser that 14 of the longest tests reach | 1089 s | 37% less |
| A change that reaches every test | as before | 0% |

The gain depends on the change. A replay of the rules on the 36 changes that were merged before, by the share of the full test time that each selected:

| Share of the full test time | Changes |
|---|---|
| Under 10% | 5 |
| 10% to 25% | 5 |
| Over 25%, not every test | 7 |
| Every test (a large feature, the project file, a line that runs at app start) | 19 |

So about half of the changes got no gain.

The selection found a real bug: with an old parser bug put back, the selected run failed on the same two tests that the original fix had changed.

## What made the wait long

1. **Every run ran every test.** The suite does not know which test reaches which code.
2. **Fresh simulators are slow for about 12 minutes.** Four simulators that boot together used 250% to 850% of the 1000% CPU for about 12 minutes with no test running. A test in those minutes took 3 to 8 times as long. One simulator that stays booted ran each test 3.5 to 4.5 times as fast as four fresh ones.
3. **Runs waited for each other.** Two agent sessions used the same simulators and the same memory budget, so the second run waited for the first.

## The method

1. **Build with coverage counters.** Pass the build setting `CLANG_COVERAGE_MAPPING=YES` to `xcodebuild build-for-testing`. Do not use `-enableCodeCoverage YES`: with that flag xcodebuild collects the coverage itself, the app does not write a file per launch, and the first launch waited 60 s in our runs.
2. **One coverage file per app launch, named after the test.** `TestImpact.swift` is the principal class of the UI test bundle. It exchanges `XCUIApplication.launch()` for a version that first sets `LLVM_PROFILE_FILE` to `<folder>/<Class>.<test>.%p.profraw%c` in the launch environment. `%c` is continuous mode: XCUITest stops the app with a kill, and a file that is written at exit would stay empty.
3. **Make the map.** After a run of every test on a clean tree, `impact.py record` merges the files of each test (`llvm-profdata`), exports the lines (`llvm-cov export -format=lcov`), and stores for each test the lines it ran. The map belongs to the commit.
4. **Select.** `impact.py plan` compares the work tree with the commit of the nearest map (`git diff -U0`) and selects each test that ran a changed line, or the line before or after it. The output is a list of `-only-testing:` arguments.
5. **Run a short selection on a simulator that is already booted.** Keep one simulator booted all day. Boot more simulators only for a full run.

A map of 175 tests and 97 source files is 52 KB.

## Use it in your project

You need Xcode with its command line tools, Python 3.9 or later, and git. Tested with Xcode 27.2. `TestImpact.swift` uses `nonisolated(unsafe)`, which needs Swift 5.10 or later.

1. Copy `impact.py` into your repository. Copy `Example/UITests/TestImpact.swift` into your UI test target.
2. Make `TestImpact` the principal class of the UI test bundle. With a generated Info.plist, add this build setting to the UI test target:

   ```
   INFOPLIST_KEY_NSPrincipalClass = TestImpact
   ```

   With your own Info.plist, add the key `NSPrincipalClass` with the value `TestImpact`.
3. Add `test-impact.json` at the root of the repository. All paths are from the root.

   ```json
   {
     "sources": ["App/Sources/", "Packages/Core/Sources/"],
     "ui_tests": "App/UITests/",
     "target": "AppUITests",
     "scope": ["App/", "Packages/"],
     "ignore": ["Packages/Core/Tests/"],
     "read_by": {}
   }
   ```

   | Key | Meaning |
   |---|---|
   | `sources` | Folders with the Swift files that are compiled into the app |
   | `ui_tests` | The folder of the UI test target |
   | `target` | The name of the UI test target |
   | `scope` | Only a change under these folders can select a UI test. A file here that is not Swift source selects every test. A `.md` file and a path with a part that starts with a dot never select a test |
   | `ignore` | Folders under `scope` that reach no UI test, such as the unit tests of a package |
   | `read_by` | A file outside the UI test folder that one UI test file reads, with the name of that UI test file |

4. Make the map. Commit first: a map is of a commit, and `record` refuses a tree with uncommitted changes under `scope`.

   ```bash
   SIM=<id of a booted simulator>      # xcrun simctl list devices booted
   OUT="$PWD/build"
   mkdir -p "$OUT/profiles"

   xcodebuild -project App.xcodeproj -scheme App -destination "platform=iOS Simulator,id=$SIM" \
       -derivedDataPath "$OUT/dd" CLANG_COVERAGE_MAPPING=YES build-for-testing

   TEST_RUNNER_TEST_IMPACT_DIR="$OUT/profiles" xcodebuild test-without-building \
       -project App.xcodeproj -scheme App -destination "platform=iOS Simulator,id=$SIM" \
       -derivedDataPath "$OUT/dd" -parallel-testing-enabled NO -resultBundlePath "$OUT/result.xcresult"

   python3 impact.py record --profiles "$OUT/profiles" \
       --app "$OUT/dd/Build/Products/Debug-iphonesimulator/App.app" --xcresult "$OUT/result.xcresult"
   ```

   To use more simulators, start one `xcodebuild test-without-building` per simulator, each with a part of the tests and its own profile folder, and give `record` one `--profiles` per folder. That is how the 175 tests ran. Xcode's own parallel clones (`-parallel-testing-enabled YES`) are not tested with this.

   xcodebuild gives the test runner each variable `TEST_RUNNER_<name>` as `<name>`. Without `TEST_IMPACT_DIR` in the environment, `TestImpact.swift` does nothing, so it can stay in the target for every run.

5. After a change, run the selection.

   ```bash
   python3 impact.py plan            # JSON: each selected test with its reasons
   python3 impact.py plan --args     # the -only-testing arguments, one per line
   ```

   `plan --args` exits with 0 and prints no line when every test must run. It exits with 3 when no test is needed, and with 2 when the config is wrong (not JSON, an unknown key, no test in `ui_tests`). `Example/demo.sh` shows how a script uses these.

Maps are stored in `~/.cache/test-impact/<folder name of the main checkout>/`, so the git worktrees of one repository share them. Two repositories with the same folder name would share a folder too: set `TEST_IMPACT_MAPS` to use another folder. The newest 4 maps are kept. `plan` looks at the newest 3 and uses the one with the fewest changed lines between its commit and your tree.

## Try the example

`Example/` is a SwiftUI app with three tabs and three UI tests. You need [XcodeGen](https://github.com/yonaskolb/XcodeGen) (`brew install xcodegen`) and one booted simulator.

```bash
git clone https://github.com/fskroes/xcuitest-impact.git
cd xcuitest-impact
Example/demo.sh map <simulator id>
```

This builds the app, runs the 3 tests and writes the map. Then change the greeting in `Example/Sources/GreetingView.swift` from `"Hello, \(name)"` to `"Hi, \(name)"` and run:

```bash
python3 impact.py plan --args
Example/demo.sh test <simulator id>
```

`plan` prints one argument, `-only-testing:ExampleUITests/GreetingTests/testGreetingShowsTheName`. `demo.sh test` runs that one test, and it fails, because the test expects "Hello, Ada".

What other changes select in the example:

| Change | Selected |
|---|---|
| The step of the counter in `CounterView.swift` | `CounterTests` |
| The text in `AboutView.swift` | `AboutTests` |
| A comment in a source file | no test |
| `UITests/Helpers.swift` (the base class of two test classes) | `CounterTests` and `GreetingTests` |
| `Example/project.yml` | every test |

## The rules

The rules are in `select` and `file_selection` of `impact.py`. They err to more tests.

- A changed line of code that can run: the tests that ran that line, or the line before or after it.
- A change of comments and blank lines only: no test.
- A changed line that cannot run (a stored property, an `import`, a `case`): every test that ran a line of that file. The lines cannot say which tests a declaration reaches.
- New lines between two lines that can run: the tests that ran one of the two. New lines at the edge of a body: every test that ran the file.
- A source file with no line in the map (declarations only): every test.
- A new source file: no test by itself. The lines that call it are changes of their own.
- A deleted source file: every test that ran it.
- A UI test file: its own tests, the tests of the subclasses of its classes, and the tests of each file that uses one of its names. The check is textual: a file uses a name when the name is a word of its text.
- `TestImpact.swift` itself: every test.
- A file under `scope` that is not Swift source (the project file, a resource, `Package.swift`): every test.
- A test that is not in the map (new, or it failed when the map was made): always selected.
- No map, or no map of a commit that git knows: every test.

## Limits

A selected run can miss a regression that a full run finds. Know these cases before you trust it:

- **Tests that depend on data, not on lines.** A changed JSON resource selects every test, but a change on a server or in a file outside `scope` selects none.
- **Lines that run only in some runs.** If a test reaches a line only now and then (a race, a retry path), the map may not tie the test to that line.
- **Dynamic dispatch through a declaration.** A new `override` or a new protocol conformance changes which code runs for tests that never ran the new lines. The rules for declarations catch the cases in the same file, not all cases.
- **Only `XCUIApplication.launch()` is recorded.** An app that a test starts with `activate()`, an app extension, and any other process write no coverage file.
- **An old map.** The selection is computed against the commit of the map. It stays correct as the branch moves, but it grows. Make a new map often.

Run every test on the main branch on a schedule (each night, or after each merge) and write the map from that run. The selected run is the fast check while you work; the full run is the safety net.

A test that failed in the full run gets no entry in the map and is selected every time. That is on purpose: a failed test stopped early, so the lines it ran are not all the lines it reaches.

## When the map stays empty

`record` prints the count of tests with coverage and a reason for each test without.

- `no coverage file`: `TestImpact` was not loaded (check `NSPrincipalClass`), or `TEST_RUNNER_TEST_IMPACT_DIR` was not set for the test run, or the test starts the app in another way than `XCUIApplication.launch()`.
- `its coverage files have no line that ran`: the app was built without `CLANG_COVERAGE_MAPPING=YES`, or no folder of `sources` matches the paths of the build.
- A file named `none.none.<pid>.profraw` is a launch outside a test (in `setUp` of the class, for example). It is ignored.

## Tests

```bash
python3 tests/test_impact.py
```

44 tests, no simulator, under one second.

## License

MIT. See `LICENSE`.
