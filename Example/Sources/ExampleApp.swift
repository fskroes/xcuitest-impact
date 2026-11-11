import SwiftUI

@main
struct ExampleApp: App {
    var body: some Scene {
        WindowGroup {
            TabView {
                CounterView()
                    .tabItem { Label("Counter", systemImage: "plus.circle") }
                GreetingView()
                    .tabItem { Label("Greeting", systemImage: "hand.wave") }
                AboutView()
                    .tabItem { Label("About", systemImage: "info.circle") }
            }
        }
    }
}
