import XCTest

final class CounterTests: ExampleTestCase {
    func testAddOneCounts() {
        open(tab: "Counter")
        app.buttons["counter.add"].tap()
        app.buttons["counter.add"].tap()
        XCTAssertEqual(app.staticTexts["counter.label"].label, "Count: 2")
    }
}
