import XCTest

/// The base class of the tests that start on a tab.
class ExampleTestCase: XCTestCase {
    let app = XCUIApplication()

    func open(tab: String) {
        continueAfterFailure = false
        app.launch()
        app.tabBars.buttons[tab].tap()
    }
}
