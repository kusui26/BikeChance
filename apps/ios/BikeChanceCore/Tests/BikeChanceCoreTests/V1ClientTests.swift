import Foundation
import Testing

@testable import BikeChanceCore

/// `/v1` を叩く層。差し替え可能な `Transport` にしてあるので、通信せずに分岐を通せる。
///
/// ここで守りたいのは 3 つ。
///   * **`Authorization` を付けない**（付けると CDN のキャッシュが効かない）
///   * **渡した矩形をそのまま送らない**（上限まで縮めて格子に丸める）
///   * 400 の本文が Problem なら、種類まで含めて拾う
@Suite("V1Client")
struct V1ClientTests {
    let base = URL(string: "https://example.test")!

    /// 記録つきの応答役。
    final class Recorder: @unchecked Sendable {
        var requests: [URLRequest] = []
    }

    func client(
        status: Int = 200,
        body: Data,
        recorder: Recorder = Recorder()
    ) -> (V1Client, Recorder) {
        let client = V1Client(baseURL: base) { request in
            recorder.requests.append(request)
            let response = HTTPURLResponse(
                url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!
            return (body, response)
        }
        return (client, recorder)
    }

    func stationsBody() throws -> Data {
        try ContractTests.fixture("stations")
    }

    @Test("Authorization を付けない（CDN キャッシュのため）")
    func doesNotSendAuthorization() async throws {
        let (client, recorder) = client(body: try stationsBody())
        _ = try await client.stations(
            in: Bbox(west: 139.76, south: 35.67, east: 139.78, north: 35.69))
        let request = try #require(recorder.requests.first)
        #expect(request.value(forHTTPHeaderField: "Authorization") == nil)
        #expect(request.httpMethod == "GET")
    }

    @Test("矩形を格子に丸めてから送る")
    func quantizesBeforeSending() async throws {
        let (client, recorder) = client(body: try stationsBody())
        _ = try await client.stations(
            in: Bbox(west: 139.7654, south: 35.6743, east: 139.7712, north: 35.6821))
        let url = try #require(recorder.requests.first?.url?.absoluteString)
        #expect(url.contains("bbox=139.76,35.67,139.78,35.69"))
    }

    @Test("大きすぎる矩形は送る前に縮める")
    func clampsBeforeSending() async throws {
        let (client, recorder) = client(body: try stationsBody())
        _ = try await client.stations(
            in: Bbox(west: 139.0, south: 35.0, east: 140.0, north: 36.0))
        let url = try #require(recorder.requests.first?.url)
        let components = try #require(URLComponents(url: url, resolvingAgainstBaseURL: false))
        let items = try #require(components.queryItems)
        let bbox = try #require(items.first { $0.name == "bbox" }?.value)
        let parts = bbox.split(separator: ",").compactMap { Double($0) }
        #expect(parts.count == 4)
        // 丸めで最大 1 量子ぶん広がる
        #expect(parts[2] - parts[0] <= Bbox.requestMaxSpanDegrees + 2 * Bbox.quantumDegrees)
    }

    @Test("system を渡したときだけ絞り込む")
    func passesTheSystemFilter() async throws {
        let (with, recorderA) = client(body: try stationsBody())
        _ = try await with.stations(
            in: Bbox(west: 139.76, south: 35.67, east: 139.78, north: 35.69), system: "docomo-cycle"
        )
        #expect(
            recorderA.requests.first?.url?.absoluteString.contains("system=docomo-cycle") == true)

        let (without, recorderB) = client(body: try stationsBody())
        _ = try await without.stations(
            in: Bbox(west: 139.76, south: 35.67, east: 139.78, north: 35.69))
        #expect(recorderB.requests.first?.url?.absoluteString.contains("system=") == false)
    }

    @Test("Problem Details を種類まで拾う")
    func surfacesProblemDetails() async throws {
        let body = try ContractTests.fixture("problem_too_many")
        let (client, _) = client(status: 400, body: body)
        await #expect(throws: V1Error.self) {
            _ = try await client.stations(
                in: Bbox(west: 139.76, south: 35.67, east: 139.78, north: 35.69))
        }
        do {
            _ = try await client.stations(
                in: Bbox(west: 139.76, south: 35.67, east: 139.78, north: 35.69))
            Issue.record("エラーになるはず")
        } catch let error as V1Error {
            #expect(error.needsNarrowerBbox)
            #expect(error.message.contains("狭めて"))
        }
    }

    @Test("Problem でない失敗はステータスだけを持つ")
    func fallsBackToTheStatusCode() async throws {
        let (client, _) = client(status: 503, body: Data("not json".utf8))
        do {
            _ = try await client.stations(
                in: Bbox(west: 139.76, south: 35.67, east: 139.78, north: 35.69))
            Issue.record("エラーになるはず")
        } catch let error as V1Error {
            #expect(error == .http(status: 503))
            #expect(!error.needsNarrowerBbox)
        }
    }

    @Test("/v1/meta を取れる")
    func fetchesMeta() async throws {
        let (client, recorder) = client(body: try ContractTests.fixture("meta"))
        let meta = try await client.meta()
        #expect(meta.apiVersion == "v1")
        #expect(recorder.requests.first?.url?.path == "/v1/meta")
    }
}
