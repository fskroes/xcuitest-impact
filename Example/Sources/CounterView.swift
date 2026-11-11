import SwiftUI

struct CounterView: View {
    @State private var count = 0

    var body: some View {
        VStack(spacing: 16) {
            Text(label(count))
                .accessibilityIdentifier("counter.label")
            Button("Add one") {
                count = next(count)
            }
            .accessibilityIdentifier("counter.add")
        }
    }

    func next(_ value: Int) -> Int {
        return value + 1
    }

    func label(_ value: Int) -> String {
        return "Count: \(value)"
    }
}
