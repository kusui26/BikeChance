/**
 * Open-Meteo から予報を取得する。**予報の URL を組み立てる唯一の場所。**
 *
 * ODPT と違って API キーが要らない（無料枠は非商用限定。開発プラン D-20・R11）ので、
 * `odpt-fetch.ts` のようなトークン封じ込めは不要である。それでも取得を 1 ファイルに
 * 閉じるのは同じ理由で、**外向きの通信をどこから出しているかを一望できるようにする**ため。
 *
 * 保存するのは応答のバイト列そのもの（W2-02）。パースも変換もしない。W4 で特徴量の
 * 定義が変わっても、保存済みのアーカイブから作り直せる。
 *
 * 複数地点は 1 回の要求にまとめられる（実測：100 地点 48 時間で HTTP 200・214 KB・2.0 秒）。
 * **1 地点だけのときは配列ではなくオブジェクトが返る**ので、保存の前に形を確かめる。
 */
import {
  OPEN_METEO_FORECAST_DAYS,
  OPEN_METEO_FORECAST_URL,
  OPEN_METEO_HOURLY_VARIABLES,
  OPEN_METEO_MODELS,
  USER_AGENT_PRODUCT,
  WEATHER_FETCH_TIMEOUT_MS,
} from "@bikechance/shared";
import { JobError, httpJobFailure, toJobFailure } from "./errors";

const HTTP_OK = 200;

/** 気象格子の 1 セル。座標は `weather_grid_cells` が丸めた値。 */
export type WeatherCell = {
  readonly lat: number;
  readonly lon: number;
};

export type WeatherFetchResponse = {
  readonly http_status: number;
  readonly bytes: number;
  /** 受信したバイト列そのもの。再直列化しない（W1 プラン §5 の 14 と同じ規律）。 */
  readonly body: Uint8Array;
  /** 応答に含まれていた地点の数。要求した格子数と一致するはず。 */
  readonly n_locations: number;
};

const joinCoordinate = (
  cells: readonly WeatherCell[],
  pick: (cell: WeatherCell) => number,
): string => cells.map((cell) => String(pick(cell))).join(",");

/**
 * 要求 URL を組み立てる。座標はカンマ区切りで、緯度と経度の順序が対応する。
 * `timezone` を渡すのは人が読むときに迷わないため。保存するのは応答そのままなので
 * 解釈は W4 で行う。
 */
const forecastUrl = (cells: readonly WeatherCell[]): string => {
  const query = new URLSearchParams({
    latitude: joinCoordinate(cells, (cell) => cell.lat),
    longitude: joinCoordinate(cells, (cell) => cell.lon),
    hourly: OPEN_METEO_HOURLY_VARIABLES.join(","),
    models: OPEN_METEO_MODELS.join(","),
    forecast_days: String(OPEN_METEO_FORECAST_DAYS),
    timezone: "Asia/Tokyo",
  });
  return `${OPEN_METEO_FORECAST_URL}?${query.toString()}`;
};

/**
 * 応答に何地点ぶん入っているかを数える。**1 地点だけのときは配列でなくオブジェクト**で
 * 返るため、両方を受ける。形が違えば取り込みの前に止める。
 */
export const countLocations = (document: unknown): number => {
  if (Array.isArray(document)) {
    return document.length;
  }
  if (typeof document === "object" && document !== null && "latitude" in document) {
    return 1;
  }
  throw new JobError({
    phase: "parse",
    error_name: "UnexpectedWeatherShape",
    http_status: null,
    message: "Open-Meteo の応答が地点の配列でもオブジェクトでもない",
  });
};

/** 1 分割ぶんの予報を取得する。失敗は `fetch` フェーズの JobError にする。 */
export const fetchWeatherBatch = async (params: {
  readonly cells: readonly WeatherCell[];
  readonly contact_email: string;
}): Promise<WeatherFetchResponse> => {
  if (params.cells.length === 0) {
    throw new JobError({
      phase: "validate",
      error_name: "EmptyWeatherBatch",
      http_status: null,
      message: "格子が空の分割は取得できない",
    });
  }

  const received = await fetch(forecastUrl(params.cells), {
    headers: {
      "User-Agent": `${USER_AGENT_PRODUCT} (+${params.contact_email})`,
      Accept: "application/json",
    },
    signal: AbortSignal.timeout(WEATHER_FETCH_TIMEOUT_MS),
    redirect: "error",
    // 予報は毎時新しくなる。Next のキャッシュに載せない（既定でオプトインだが明示する）
    cache: "no-store",
  }).catch((cause: unknown) => {
    throw new JobError(toJobFailure({ phase: "fetch", cause }));
  });

  const body = new Uint8Array(await received.arrayBuffer());
  if (received.status !== HTTP_OK) {
    throw new JobError(
      httpJobFailure({
        phase: "fetch",
        http_status: received.status,
        detail: `Open-Meteo が想定外のステータスを返した (${received.status})`,
      }),
    );
  }
  return {
    http_status: received.status,
    bytes: body.byteLength,
    body,
    n_locations: countLocations(JSON.parse(new TextDecoder().decode(body))),
  };
};
