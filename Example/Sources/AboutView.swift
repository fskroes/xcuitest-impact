import SwiftUI

struct AboutView: View {
    var body: some View {
        Text(aboutText())
            .accessibilityIdentifier("about.text")
    }

    func aboutText() -> String {
        return "An example app for test selection"
    }
}
