import XCTest

class ExampleTestCase: XCTestCase {
    let app = XCUIApplication()

    func open(tab: String) {
        continueAfterFailure = false
        app.launch()
        app.tabBars.buttons[tab].tap()
    }
}
