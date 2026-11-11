import XCTest

final class GreetingTests: ExampleTestCase {
    func testGreetingShowsTheName() {
        open(tab: "Greeting")
        let field = app.textFields["greeting.name"]
        field.tap()
        field.typeText("Ada")
        app.buttons["greeting.greet"].tap()
        XCTAssertEqual(app.staticTexts["greeting.text"].label, "Hello, Ada")
    }
}
