/// お気に入りのシートに出すクレジット（W7 プランの W7-08、W6 プランの所見 219）。
///
/// お気に入りはシートで開くので、地図の下の 1 行のクレジット（`CreditFooter`）が隠れる。
/// 行は ODPT のデータ（台数と確率）を出すので、**この画面にもクレジットを出す**
/// （表示義務は「その画面からクレジットが見えるか・行けるか」で確かめる）。
///
/// **文言は応答のまま**（W6-31）。行は `/v1/stations/{system}/{id}` の応答から作るので、
/// その応答の前置きと `attribution` を使う——サーバーの変更は要らない。
public struct FavoritesCredits: Equatable, Sendable {
    /// 「このアプリは、以下の著作物を改変して利用しています。」。**クレジットの前に出す。**
    /// PR J より前のサーバーは返さないので nil がある
    public let preface: String?
    /// 系統ごとに 1 行。**並びは `system_id` の順**（お気に入りを並べ替えても動かない）
    public let credits: [String]

    /// 行に出している応答から集める。**1 つも無ければ nil**——出しているデータが無ければ、
    /// 出す出典も無い。前置きは、返した応答のものを使う（古い応答と混ざっても出す）。
    public static func collect(from details: [StationDetailResponse]) -> FavoritesCredits? {
        let bySystem = Dictionary(
            details.flatMap(\.attribution).map { ($0.systemID, $0) },
            uniquingKeysWith: { first, _ in first })
        guard !bySystem.isEmpty else { return nil }
        return FavoritesCredits(
            preface: details.lazy.compactMap(\.attributionPreface).first,
            credits: bySystem.keys.sorted().compactMap { bySystem[$0]?.credit })
    }
}
