#!/bin/bash
set -euo pipefail

MODE="${1:?map or test}"
SIM="${2:?the id of a booted simulator}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$ROOT/build"
DEST="platform=iOS Simulator,id=$SIM"
cd "$ROOT"

xcodegen generate --quiet --spec Example/project.yml

build() {
    xcodebuild -project Example/Example.xcodeproj -scheme Example -destination "$DEST" \
        -derivedDataPath "$BUILD/dd" CLANG_COVERAGE_MAPPING=YES build-for-testing -quiet
}

run_tests() {
    rm -rf "$BUILD/result.xcresult"
    xcodebuild test-without-building -project Example/Example.xcodeproj -scheme Example -destination "$DEST" \
        -derivedDataPath "$BUILD/dd" -parallel-testing-enabled NO -resultBundlePath "$BUILD/result.xcresult" "$@"
}

case "$MODE" in
map)
    build
    rm -rf "$BUILD/profiles"
    mkdir -p "$BUILD/profiles"
    TEST_RUNNER_TEST_IMPACT_DIR="$BUILD/profiles" run_tests || echo "A test failed; the map leaves it out."
    python3 impact.py record --profiles "$BUILD/profiles" \
        --app "$BUILD/dd/Build/Products/Debug-iphonesimulator/Example.app" --xcresult "$BUILD/result.xcresult"
    ;;
test)
    status=0
    selected="$(python3 impact.py plan --args)" || status=$?
    if [ "$status" -eq 3 ]; then
        echo "No UI test can reach this change."
        exit 0
    fi
    [ "$status" -eq 0 ] || exit "$status"
    build
    # shellcheck disable=SC2086
    run_tests $selected
    ;;
*)
    echo "usage: Example/demo.sh map|test <simulator id>" >&2
    exit 2
    ;;
esac
