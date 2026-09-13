import BikeChanceCore
import MapKit
import SwiftUI

/// 目的地を決める（開発プラン §9.2）。**地点を探して、その最寄りポートを選ぶ**の 2 段。
///
/// **ポートの名前だけで探させない。** 利用者が知っているのは「行き先」であって
/// ポート名ではない（「C1-92.エステムプラザ愛宕虎ノ門レジデンス」を覚えている人はいない）。
///
/// **同じ事業者のポートだけを出す。** 行程は 1 系統で完結する（W4-21）ので、
/// 別事業者のポートを選べてしまうと、選んだあとで断られることになる。
///
/// **位置情報は送らない。** 地点の座標は矩形に丸めてから `/v1/stations` に渡る
/// （`V1Client` が量子化する。CLAUDE.md §5）。
struct DestinationPicker: View {
    /// 出発ポート。**系統と、探す中心**をここから取る。
    let origin: StationCurrent
    let client: V1Client
    let now: Date
    /// 選ばれたポートを返して閉じる。
    let choose: (StationCurrent) -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var query = ""
    @State private var completer = SearchCompleter()
    @State private var place: MKMapItem?
    @State private var candidates: [StationCurrent] = []
    @State private var status: Status = .idle

    private enum Status: Equatable {
        case idle
        case searching
        case failed(String)
    }

    /// 地点の周りを探す矩形の 1 辺（度）。**約 2.2 km 四方。**
    ///
    /// 代替候補の半径（400 m）より十分広く取る——目的地のすぐそばにポートが無いことは
    /// 珍しくない。広げすぎると件数の上限（1,000 件）に当たるので、`Bbox` の
    /// 要求上限（0.1 度）よりは十分小さくしてある。
    private static let searchSpanDegrees = 0.02
    /// 出す候補の数。**5 件**——多いと選べず、少ないと「近くに無い」に見える。
    private static let candidateLimit = 5

    var body: some View {
        NavigationStack {
            List {
                if let place {
                    placeSection(place)
                } else {
                    suggestionSection
                }
            }
            .searchable(text: $query, prompt: "目的地を検索")
            .onChange(of: query) { _, text in
                completer.update(query: text, around: origin.coordinate)
                if text.isEmpty { place = nil }
            }
            .navigationTitle("どこへ行きますか")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("閉じる") { dismiss() }
                }
            }
        }
    }

    /// 検索の候補。**`MKLocalSearchCompleter` は打つたびに返る**ので、そのまま並べる。
    @ViewBuilder
    private var suggestionSection: some View {
        if completer.suggestions.isEmpty {
            Section {
                Text(query.isEmpty ? "駅名・施設名・住所で探せます。" : "見つかりませんでした。")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }
        } else {
            Section("行き先") {
                ForEach(completer.suggestions, id: \.self) { suggestion in
                    Button {
                        Task { await resolve(suggestion) }
                    } label: {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(suggestion.title).font(.body)
                            if !suggestion.subtitle.isEmpty {
                                Text(suggestion.subtitle)
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }

    /// 選んだ地点の最寄りポート。**近い順**（`MarkerSelection.closest`）。
    @ViewBuilder
    private func placeSection(_ place: MKMapItem) -> some View {
        Section {
            LabeledContent(place.name ?? "選んだ場所") {
                Button("変更") { self.place = nil }
            }
        }
        switch status {
        case .searching:
            Section { ProgressView().frame(maxWidth: .infinity) }
        case .failed(let message):
            Section {
                Label(message, systemImage: "exclamationmark.triangle").foregroundStyle(.orange)
            }
        case .idle:
            if candidates.isEmpty {
                Section {
                    Text("この場所の近くに、\(origin.systemID) のポートが見つかりませんでした。")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }
            } else {
                Section("近くのポート") {
                    ForEach(candidates) { station in
                        Button {
                            choose(station)
                            dismiss()
                        } label: {
                            candidateRow(station)
                        }
                        .buttonStyle(.plain)
                        // **出発と同じポートは選ばせない**（選んでも 400 になる）
                        .disabled(station.id == origin.id)
                        .opacity(station.id == origin.id ? 0.4 : 1)
                    }
                }
            }
        }
    }

    private func candidateRow(_ station: StationCurrent) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(station.name ?? "（名称未取得）").font(.body)
            // **いまの空き。** 確率は行程を調べたあとに出る（ここではまだ時刻が決まっていない）
            Text("いま 返せる \(station.countText(for: .returnBike, freshness: freshness(station)))")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func freshness(_ station: StationCurrent) -> Freshness {
        station.freshness(feed: nil, now: now)
    }

    /// 候補を 1 つ選んだ。**地点を引いてから、その周りのポートを引く。**
    private func resolve(_ suggestion: MKLocalSearchCompletion) async {
        status = .searching
        do {
            let search = MKLocalSearch(request: MKLocalSearch.Request(completion: suggestion))
            let found = try await search.start()
            guard let item = found.mapItems.first else {
                status = .failed("場所を特定できませんでした。")
                return
            }
            place = item
            await loadPorts(around: item.location.coordinate)
        } catch {
            status = .failed("場所を検索できませんでした。")
        }
    }

    private func loadPorts(around coordinate: CLLocationCoordinate2D) async {
        let center = Coordinate(
            latitude: coordinate.latitude, longitude: coordinate.longitude)
        do {
            let response = try await client.stations(
                in: center.square(spanDegrees: Self.searchSpanDegrees),
                system: origin.systemID
            )
            candidates = MarkerSelection.closest(
                response.stations, to: center, limit: Self.candidateLimit)
            status = .idle
        } catch let error as V1Error {
            status = .failed(error.message)
        } catch {
            status = .failed("ポートを取得できませんでした。")
        }
    }
}

/// `MKLocalSearchCompleter` の薄い包み。**打つたびに候補が入れ替わる。**
///
/// `MKLocalSearchCompleterDelegate` は `NSObject` を要求するので、`@Observable` の
/// 値型にはできない。**変わるのは候補の配列だけ**なので、そこだけを公開する。
@MainActor
@Observable
final class SearchCompleter: NSObject, MKLocalSearchCompleterDelegate {
    private(set) var suggestions: [MKLocalSearchCompletion] = []
    private let completer = MKLocalSearchCompleter()

    override init() {
        super.init()
        completer.delegate = self
        // **住所と目印だけ**。「クエリ」（＝『近くのカフェ』のような検索語）は、
        // 座標が 1 点に定まらないので目的地にならない
        completer.resultTypes = [.address, .pointOfInterest]
    }

    /// 探す範囲を出発ポートの周りに寄せる。**遠くの同名の駅を上に出さない。**
    func update(query: String, around origin: Coordinate) {
        guard !query.isEmpty else {
            suggestions = []
            return
        }
        completer.region = MKCoordinateRegion(
            center: CLLocationCoordinate2D(
                latitude: origin.latitude, longitude: origin.longitude),
            span: MKCoordinateSpan(latitudeDelta: 0.5, longitudeDelta: 0.5)
        )
        completer.queryFragment = query
    }

    // **`MKLocalSearchCompleterDelegate` の呼び出しは主スレッドに来る**（MapKit が
    // そう注釈している）。`nonisolated` にして `Task { @MainActor }` で渡し直すと、
    // `MKLocalSearchCompletion` が `Sendable` でないぶん境界をまたげない
    func completerDidUpdateResults(_ completer: MKLocalSearchCompleter) {
        suggestions = completer.results
    }

    func completer(_ completer: MKLocalSearchCompleter, didFailWithError error: Error) {
        // **理由は出さない。** 打っている途中の失敗は毎回起きうる（通信・打ち間違い）
        suggestions = []
    }
}
