import SwiftUI

struct GreetingView: View {
    @State private var name = ""
    @State private var greeting = ""

    var body: some View {
        VStack(spacing: 16) {
            TextField("Name", text: $name)
                .textFieldStyle(.roundedBorder)
                .accessibilityIdentifier("greeting.name")
            Button("Greet") {
                greeting = greet(name)
            }
            .accessibilityIdentifier("greeting.greet")
            Text(greeting)
                .accessibilityIdentifier("greeting.text")
        }
        .padding()
    }

    func greet(_ name: String) -> String {
        if name.isEmpty {
            return "Hello, stranger"
        }
        return "Hello, \(name)"
    }
}
