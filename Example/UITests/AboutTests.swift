import XCTest

/// Uses no helper of the other tests: a change to Helpers.swift does not select it.
final class AboutTests: XCTestCase {
    func testAboutShowsItsText() {
        let example = XCUIApplication()
        example.launch()
        example.tabBars.buttons["About"].tap()
        XCTAssertEqual(example.staticTexts["about.text"].label, "An example app for test selection")
    }
}
