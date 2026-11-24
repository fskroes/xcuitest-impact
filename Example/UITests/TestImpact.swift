import ObjectiveC
import XCTest

@objc(TestImpact)
final class TestImpact: NSObject, XCTestObservation {
    static let folder = ProcessInfo.processInfo.environment["TEST_IMPACT_DIR"].flatMap { $0.isEmpty ? nil : $0 }
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

    static func fileName(_ name: String) -> String {
        let words = name.trimmingCharacters(in: CharacterSet(charactersIn: "-[]")).split(separator: " ")
        guard words.count == 2, let type = words[0].split(separator: ".").last else { return "none.none" }
        return "\(type).\(words[1])".filter { $0.isLetter || $0.isNumber || $0 == "." || $0 == "_" }
    }
}

extension XCUIApplication {
    @objc func launchRecorded() {
        if let folder = TestImpact.folder {
            launchEnvironment["LLVM_PROFILE_FILE"] = "\(folder)/\(TestImpact.current).%p.profraw%c"
        }
        launchRecorded()
    }
}
