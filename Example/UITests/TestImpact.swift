import ObjectiveC
import XCTest

/// Records which lines of the app each UI test runs. Copy this file into your UI test target and make it the
/// principal class of the test bundle (`NSPrincipalClass` = `TestImpact` in the Info.plist of the UI test target, or
/// the build setting `INFOPLIST_KEY_NSPrincipalClass = TestImpact`), so it exists before the first test starts.
///
/// It does nothing unless the test runner has `TEST_IMPACT_DIR` in its environment. xcodebuild gives the runner a
/// variable `X` when its own environment has `TEST_RUNNER_X`.
///
/// Every launch of the app by a test then writes its own coverage file `<Class>.<test>.<pid>.profraw` in that folder
/// (the app must be built with `CLANG_COVERAGE_MAPPING=YES`). The launch is the one place every test goes through,
/// so `launch()` itself is exchanged for a version that names the file first. `%c` makes the app write its counters
/// as it runs: XCUITest stops the app with a kill, and a file that is written at exit would stay empty.
@objc(TestImpact)
final class TestImpact: NSObject, XCTestObservation {
    /// The folder of the coverage files, or nil when this run records nothing.
    static let folder = ProcessInfo.processInfo.environment["TEST_IMPACT_DIR"].flatMap { $0.isEmpty ? nil : $0 }
    /// `<Class>.<test>` of the test that runs now. XCTest runs the tests and calls the observer on the main thread.
    nonisolated(unsafe) static var current = "none.none"

    override init() {
        super.init()
        guard Self.folder != nil else { return }
        XCTestObservationCenter.shared.addTestObserver(self)
        guard let launch = class_getInstanceMethod(XCUIApplication.self, #selector(XCUIApplication.launch)),
              let recorded = class_getInstanceMethod(XCUIApplication.self, #selector(XCUIApplication.launchRecorded))
        else { return }
        method_exchangeImplementations(launch, recorded)
    }

    func testCaseWillStart(_ testCase: XCTestCase) {
        Self.current = Self.fileName(testCase.name)
    }

    /// "-[MyUITests.SellTests testX]" or "-[SellTests testX]" gives "SellTests.testX".
    static func fileName(_ name: String) -> String {
        let words = name.trimmingCharacters(in: CharacterSet(charactersIn: "-[]")).split(separator: " ")
        guard words.count == 2, let type = words[0].split(separator: ".").last else { return "none.none" }
        return "\(type).\(words[1])".filter { $0.isLetter || $0.isNumber || $0 == "." || $0 == "_" }
    }
}

extension XCUIApplication {
    /// After the exchange in `TestImpact.init`, a call of `launch()` runs this, and the call of `launchRecorded()`
    /// in it runs the real `launch()`.
    @objc func launchRecorded() {
        if let folder = TestImpact.folder {
            launchEnvironment["LLVM_PROFILE_FILE"] = "\(folder)/\(TestImpact.current).%p.profraw%c"
        }
        launchRecorded()
    }
}
