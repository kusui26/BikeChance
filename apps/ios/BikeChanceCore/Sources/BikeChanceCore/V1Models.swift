import Foundation

/// `/v1` の応答。**正は `packages/shared/src/api.ts` の Zod スキーマ**で、
/// ここはその写し。契約テスト（`Fixtures/` の実応答をデコードする）でずれを検出する。
///
/// **NULL の意味を型で表す**（データ辞書 §4）。
///   * `name` / `capacity` … 属性をまだ取れていない（新しいポートは最大 1 日）
///   * `bikes` / `docks` / `isRenting` など … 一度も観測されていない。**0 や false と違う**
///   * `observedAt` … 最新のフィードに現れなかった。値がいつのものか分からない
public struct StationCurrent: Decodable, Hashable, Sendable, Identifiable {
    public let systemID: String
    public let stationID: String
    public let name: String?
    public let latitude: Double
    public let longitude: Double
    public let capacity: Int?
    public let bikes: Int?
    public let docks: Int?
    public let isInstalled: Bool?
    public let isRenting: Bool?
    public let isReturning: Bool?
    public let isPresent: Bool
    public let observedAt: Date?
    public let lastChangedAt: Date

    /// システムをまたいで一意。`station_id` はシステム内でしか一意でない。
    public var id: String { "\(systemID)/\(stationID)" }

    private enum CodingKeys: String, CodingKey {
        case systemID = "system_id"
        case stationID = "station_id"
        case name
        case latitude = "lat"
        case longitude = "lon"
        case capacity
        case bikes
        case docks
        case isInstalled = "is_installed"
        case isRenting = "is_renting"
        case isReturning = "is_returning"
        case isPresent = "is_present"
        case observedAt = "observed_at"
        case lastChangedAt = "last_changed_at"
    }
}

/// フィードの鮮度。**閾値もサーバーが返す**ので、同じ判定を端末側で再現できる。
public struct FeedStatus: Decodable, Equatable, Sendable, Identifiable {
    public let systemID: String
    public let displayName: String
    public let dataUpdatedAt: Date?
    public let expectedCadenceSeconds: Int
    public let staleAfterSeconds: Int
    public let isStale: Bool
    /// `capacity` が固定のラック数ではなく動的値か（ドコモが true）。
    public let capacityIsDynamic: Bool

    public var id: String { systemID }

    private enum CodingKeys: String, CodingKey {
        case systemID = "system_id"
        case displayName = "display_name"
        case dataUpdatedAt = "data_updated_at"
        case expectedCadenceSeconds = "expected_cadence_s"
        case staleAfterSeconds = "stale_after_s"
        case isStale = "stale"
        case capacityIsDynamic = "capacity_is_dynamic"
    }
}

/// CC BY 4.0 の表示に要る。**応答だけで表示を完結できる**ようにサーバーが毎回返す。
public struct Attribution: Decodable, Equatable, Sendable, Identifiable {
    public let systemID: String
    public let provider: String
    public let dataset: String
    public let license: String
    public let licenseURL: URL

    public var id: String { systemID }

    private enum CodingKeys: String, CodingKey {
        case systemID = "system_id"
        case provider
        case dataset
        case license
        case licenseURL = "license_url"
    }

    /// CC BY 4.0「改変して利用する場合」の 1 行。
    public var credit: String {
        "\(provider)、\(dataset)、\(license)（\(licenseURL.absoluteString)）"
    }
}

public struct BboxEnvelope: Decodable, Equatable, Sendable {
    public let west: Double
    public let south: Double
    public let east: Double
    public let north: Double

    public var bbox: Bbox { Bbox(west: west, south: south, east: east, north: north) }
}

public struct StationsResponse: Decodable, Equatable, Sendable {
    public let apiVersion: String
    public let generatedAt: Date
    /// **実際に検索された矩形。** 要求した矩形を格子に外側へ丸めたもの。
    public let bbox: BboxEnvelope
    public let count: Int
    public let isStale: Bool
    public let feeds: [FeedStatus]
    public let stations: [StationCurrent]
    public let attribution: [Attribution]

    private enum CodingKeys: String, CodingKey {
        case apiVersion = "api_version"
        case generatedAt = "generated_at"
        case bbox
        case count
        case isStale = "stale"
        case feeds
        case stations
        case attribution
    }
}

public struct MetaResponse: Decodable, Equatable, Sendable {
    public let apiVersion: String
    public let generatedAt: Date
    public let isStale: Bool
    public let modelVersion: String?
    public let feeds: [FeedStatus]
    public let attribution: [Attribution]
    /// ODPT 開発者ガイドライン 3.1 の通知文。**表示義務がある。**
    public let notice: String
    /// 予測に関する免責。
    public let disclaimer: String

    private enum CodingKeys: String, CodingKey {
        case apiVersion = "api_version"
        case generatedAt = "generated_at"
        case isStale = "stale"
        case modelVersion = "model_version"
        case feeds
        case attribution
        case notice
        case disclaimer
    }
}

/// エラー応答（RFC 9457）。`code` で分岐する。
public struct Problem: Decodable, Equatable, Sendable, Error {
    public let type: String
    public let title: String
    public let status: Int
    public let detail: String
    public let code: String

    /// 「地図を拡大してください」と言うべき状態か。
    public var needsNarrowerBbox: Bool {
        code == "too_many_stations" || code == "bbox_too_large"
    }
}
