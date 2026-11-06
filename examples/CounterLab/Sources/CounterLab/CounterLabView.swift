import Foundation
import OSLog
import SwiftUI
import UIKit

private let counterLogger = Logger(
    subsystem: "com.bvolpato.limulator.counterlab",
    category: "counter"
)

struct CounterLabView: View {
    @AppStorage("savedCount") private var count = 0
    @Environment(\.horizontalSizeClass) private var horizontalSizeClass
    @Environment(\.scenePhase) private var scenePhase
    @State private var note = ""
    @State private var deviceMetrics: DeviceMetrics?
    @State private var networkStatus = NetworkStatus.idle

    var body: some View {
        NavigationStack {
            ZStack {
                LinearGradient(
                    colors: [Color.cyan.opacity(0.18), Color(uiColor: .systemBackground)],
                    startPoint: .topLeading,
                    endPoint: .center
                )
                .ignoresSafeArea()

                ScrollView {
                    appContent
                    .padding(20)
                    .frame(maxWidth: 1040)
                    .frame(maxWidth: .infinity)
                }
            }
            .navigationTitle("Counter Lab")
            .navigationBarTitleDisplayMode(.inline)
            .onAppear(perform: refreshDeviceMetrics)
            .onChange(of: scenePhase) { _, phase in
                if phase == .active {
                    refreshDeviceMetrics()
                }
            }
        }
    }

    @ViewBuilder
    private var appContent: some View {
        if horizontalSizeClass == .regular {
            HStack(alignment: .top, spacing: 24) {
                VStack(alignment: .leading, spacing: 24) {
                    introduction
                    counterCard
                }
                .frame(maxWidth: .infinity)

                VStack(alignment: .leading, spacing: 20) {
                    noteCard
                    deviceProfileCard
                    networkCard
                    detailsLink
                }
                .frame(maxWidth: .infinity)
            }
        } else {
            VStack(alignment: .leading, spacing: 24) {
                introduction
                counterCard
                noteCard
                deviceProfileCard
                networkCard
                detailsLink
            }
        }
    }

    private var introduction: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label("A tiny native app", systemImage: "sparkles")
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(.cyan)

            Text("Make a little progress.")
                .font(.largeTitle.bold())
                .foregroundStyle(.primary)
                .accessibilityIdentifier("counterlab.heading")

            Text("Tap to count, add a note, and see what stays saved.")
                .font(.body)
                .foregroundStyle(.secondary)
        }
        .padding(.top, 12)
    }

    private var counterCard: some View {
        VStack(spacing: 20) {
            VStack(spacing: 6) {
                Text("SAVED COUNT")
                    .font(.caption.weight(.bold))
                    .tracking(1.5)
                    .foregroundStyle(.secondary)

                Text(count.formatted())
                    .font(.system(size: 72, weight: .bold, design: .rounded))
                    .monospacedDigit()
                    .contentTransition(.numericText())
                    .foregroundStyle(.primary)
                    .accessibilityIdentifier("counterlab.counter.value")
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 12)

            Button(action: increment) {
                Label("Add one", systemImage: "plus")
                    .font(.headline)
                    .frame(maxWidth: .infinity, minHeight: 58)
            }
            .buttonStyle(.borderedProminent)
            .tint(.cyan)
            .accessibilityIdentifier("counterlab.increment")

            Button(action: reset) {
                Label("Reset count", systemImage: "arrow.counterclockwise")
                    .font(.headline)
                    .frame(maxWidth: .infinity, minHeight: 58)
            }
            .buttonStyle(.bordered)
            .accessibilityIdentifier("counterlab.reset")
        }
        .padding(22)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 28))
        .overlay {
            RoundedRectangle(cornerRadius: 28)
                .strokeBorder(.white.opacity(0.55), lineWidth: 1)
                .allowsHitTesting(false)
        }
    }

    private var noteCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            Label("Your note", systemImage: "pencil.line")
                .font(.headline)

            TextField("What are you counting today?", text: $note, axis: .vertical)
                .lineLimit(2...4)
                .padding(14)
                .background(Color(uiColor: .secondarySystemBackground), in: RoundedRectangle(cornerRadius: 16))
                .accessibilityIdentifier("counterlab.note")
        }
        .padding(18)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 24))
    }

    private var deviceProfileCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            Label("This device", systemImage: "iphone.gen3")
                .font(.headline)

            Text("Read from the active UIKit screen.")
                .font(.subheadline)
                .foregroundStyle(.secondary)

            profileRow(
                "Interface idiom",
                value: deviceMetrics?.idiom ?? "Waiting for active screen",
                identifier: "counterlab.device.idiom"
            )
            profileRow(
                "Screen size",
                value: deviceMetrics?.pointSize ?? "Unavailable",
                identifier: "counterlab.device.point_size"
            )
            profileRow(
                "Display scale",
                value: deviceMetrics?.displayScale ?? "Unavailable",
                identifier: "counterlab.device.display_scale"
            )
        }
        .padding(18)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 24))
    }

    private func profileRow(_ title: String, value: String, identifier: String) -> some View {
        HStack(spacing: 12) {
            Text(title)
                .foregroundStyle(.secondary)
            Spacer(minLength: 8)
            Text(value)
                .font(.subheadline.weight(.semibold).monospacedDigit())
                .multilineTextAlignment(.trailing)
                .foregroundStyle(.primary)
        }
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier(identifier)
    }

    private var networkCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            Label("HTTPS connection", systemImage: "network")
                .font(.headline)

            Text("Makes one read-only request to example.com. No account or app data is sent.")
                .font(.subheadline)
                .foregroundStyle(.secondary)

            Text(networkStatus.message)
                .font(.subheadline.weight(.medium))
                .foregroundStyle(networkStatus.color)
                .accessibilityIdentifier("counterlab.network.status")

            Button(action: checkNetworkConnection) {
                Label(
                    networkStatus == .checking ? "Checking connection" : "Check connection",
                    systemImage: networkStatus == .checking ? "arrow.triangle.2.circlepath" : "arrow.up.right"
                )
                .font(.headline)
                .frame(maxWidth: .infinity, minHeight: 54)
            }
            .buttonStyle(.bordered)
            .disabled(networkStatus == .checking)
            .accessibilityIdentifier("counterlab.network.check")
        }
        .padding(18)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 24))
    }

    private var detailsLink: some View {
        NavigationLink {
            CounterDetailView(count: $count, note: $note)
        } label: {
            HStack(spacing: 12) {
                Image(systemName: "list.bullet.rectangle.portrait")
                    .font(.title3)
                    .foregroundStyle(.cyan)
                VStack(alignment: .leading, spacing: 3) {
                    Text("Open details")
                        .font(.headline)
                        .foregroundStyle(.primary)
                    Text("See the current saved values")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Image(systemName: "chevron.right")
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(.tertiary)
            }
            .padding(18)
            .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 22))
            .contentShape(RoundedRectangle(cornerRadius: 22))
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier("counterlab.details.link")
    }

    private func increment() {
        let previous = count
        count += 1
        counterLogger.info(
            "Counter incremented from \(previous, privacy: .public) to \(count, privacy: .public)"
        )
    }

    private func reset() {
        let previous = count
        count = 0
        counterLogger.info(
            "Counter reset from \(previous, privacy: .public) to \(count, privacy: .public)"
        )
    }

    private func refreshDeviceMetrics() {
        guard let screen = UIApplication.shared.connectedScenes
            .compactMap({ $0 as? UIWindowScene })
            .first(where: { $0.activationState == .foregroundActive })?.screen
        else {
            return
        }

        let idiom = UIDevice.current.userInterfaceIdiom
        let idiomName: String
        switch idiom {
        case .phone:
            idiomName = "iPhone"
        case .pad:
            idiomName = "iPad"
        case .tv:
            idiomName = "Apple TV"
        case .carPlay:
            idiomName = "CarPlay"
        case .mac:
            idiomName = "Mac"
        case .vision:
            idiomName = "Apple Vision"
        case .unspecified:
            idiomName = "Unspecified"
        @unknown default:
            idiomName = "Unknown"
        }

        deviceMetrics = DeviceMetrics(
            idiom: idiomName,
            pointSize: "\(Int(screen.bounds.width.rounded())) × \(Int(screen.bounds.height.rounded())) pt",
            displayScale: "\(screen.scale.formatted())×"
        )
    }

    private func checkNetworkConnection() {
        guard networkStatus != .checking else { return }
        networkStatus = .checking

        Task { @MainActor in
            guard let url = URL(string: "https://example.com") else {
                networkStatus = .failure("The HTTPS endpoint URL is invalid")
                return
            }

            var request = URLRequest(url: url)
            request.httpMethod = "GET"
            request.cachePolicy = .reloadIgnoringLocalCacheData
            request.timeoutInterval = 20

            do {
                let (_, response) = try await URLSession.shared.data(for: request)
                guard let response = response as? HTTPURLResponse else {
                    networkStatus = .failure("Received a non-HTTP response")
                    return
                }
                networkStatus = .response(response.statusCode)
            } catch {
                networkStatus = .failure(error.localizedDescription)
            }
        }
    }
}

private struct DeviceMetrics {
    let idiom: String
    let pointSize: String
    let displayScale: String
}

private enum NetworkStatus: Equatable {
    case idle
    case checking
    case response(Int)
    case failure(String)

    var message: String {
        switch self {
        case .idle:
            "Not checked yet"
        case .checking:
            "Connecting to example.com…"
        case let .response(statusCode) where (200..<300).contains(statusCode):
            "Connected: HTTP \(statusCode)"
        case let .response(statusCode):
            "Server returned HTTP \(statusCode)"
        case let .failure(message):
            "Connection failed: \(message)"
        }
    }

    var color: Color {
        switch self {
        case .response(200..<300):
            .green
        case .failure, .response:
            .red
        case .idle, .checking:
            .secondary
        }
    }
}

private struct CounterDetailView: View {
    @Binding var count: Int
    @Binding var note: String

    var body: some View {
        Form {
            Section("Saved count") {
                LabeledContent("Current value", value: count.formatted())
                    .accessibilityIdentifier("counterlab.details.count")
            }

            Section("Your note") {
                Text(note.isEmpty ? "No note yet" : note)
                    .accessibilityIdentifier("counterlab.details.note")
                TextField("Edit your note", text: $note, axis: .vertical)
                    .lineLimit(2...4)
                    .accessibilityIdentifier("counterlab.details.note.input")
            }
        }
        .navigationTitle("Details")
        .navigationBarTitleDisplayMode(.inline)
    }
}
