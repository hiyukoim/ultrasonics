# Soundiiz バックエンド 実装仕様書(クローン構築用)

姉妹ドキュメント `soundiiz-api-spec-for-bolt.md`(API表面=エンドポイント/スキーマ)と対になる、**内部設計**の仕様書。
API仕様書が「何を返すか」なら、こちらは「どう作るか」— DBスキーマ・マッチングエンジン・ジョブ基盤・プラットフォーム連携層・課金。

> 凡例:  **[HAR実証]** = HARから直接確認できた事実 /  **[推論]** = HARの挙動から合理的に導いた設計判断(実装者が変えてよい)。

---

## 0. これがSoundiizの本質

Soundiizは要するに **「N個の音楽サービスの差異を1つの正規化モデルに吸収し、その間で楽曲メタデータを転送するETLエンジン」**。
UIやAPIは薄く、価値の9割は以下の3つに集約される:

1. **プラットフォーム連携層**(66サービス分のアダプタ + OAuthトークン管理)← 最も工数がかかる
2. **マッチング/解決エンジン**(サービスAの曲をサービスBのカタログ上の曲に対応付ける)← 最も差別化が効く
3. **非同期ジョブ基盤**(バッチ変換 + cronスケジュール同期)← 規模とUXを支える

クローンを作るなら、この3つを軸に設計する。API/認証/課金は定番の作りで足りる。

---

## 1. システムアーキテクチャ

```
                    ┌─────────────┐
   Browser  ───────▶│  API server │  (nginx + app: REST/webapi)  [HAR実証: nginx + Google Cloud]
                    └──────┬──────┘
                           │ enqueue job
                    ┌──────▼──────┐
                    │  Job queue  │  (Redis/BullMQ, SQS, etc.)
                    └──────┬──────┘
              ┌────────────┼────────────┐
        ┌─────▼─────┐ ┌────▼────┐ ┌─────▼──────┐
        │  Worker   │ │ Worker  │ │  Scheduler │  (cron → enqueue scheduled tasks)
        └─────┬─────┘ └────┬────┘ └─────┬──────┘
              │            │            │
        ┌─────▼────────────▼────────────▼─────┐
        │        Platform Adapter Layer         │  Spotify / AppleMusic / YTMusic / ...(66)
        └───────────────────┬───────────────────┘
                            │  OAuth-authenticated calls (rate-limited)
                    ┌───────▼───────┐        ┌──────────────┐
                    │  External APIs │        │  PostgreSQL  │  (users, tokens, matchings, jobs, ...)
                    └───────────────┘        └──────────────┘
```

**構成要素:**
- **API server** — 同期処理(ライブラリ読み取り・タスクCRUD)を直接、重い処理(変換)はキューに投げて `job_id`/`batch_id` を即返す。
- **Worker** — キューを消費し、アダプタ経由で外部APIを叩く。1曲ずつ解決してdestに書く。進捗をDBに記録。
- **Scheduler** — cronで `scheduled_tasks` を走査し `next_exec <= now` のものをキューに投入。
- **Adapter layer** — 全プラットフォームを共通インターフェースに抽象化(§3)。
- **DB** — PostgreSQL推奨(JSONB列でプラットフォーム固有メタを吸収できる)。

---

## 2. データベーススキーマ

API仕様書のレスポンス形状から逆算した永続化モデル。**[推論]**(HARはAPI応答しか見せないため、テーブル設計は実装者の裁量。ただし応答フィールドと整合させてある)。

### 2.1 users
```sql
CREATE TABLE users (
  id                    BIGSERIAL PRIMARY KEY,   -- [HAR実証] 数値ID (webapi/me.id)
  username              TEXT UNIQUE NOT NULL,     -- [HAR実証] メール=usernameだった
  email                 TEXT UNIQUE NOT NULL,
  password_hash         TEXT,                     -- ソーシャルログインのみなら null 可
  locale                TEXT DEFAULT 'en',        -- [HAR実証]
  timezone              TEXT DEFAULT 'UTC',       -- [HAR実証] "Europe/Berlin"
  notifications         TEXT[] DEFAULT '{}',      -- [HAR実証] {BATCH_END,TASK_ERROR,PLATFORM_DISCONNECTED}
  is_two_fa_enabled     BOOLEAN DEFAULT false,    -- [HAR実証]
  -- 課金 (§8)
  subscription_plan     TEXT,                     -- [HAR実証] 'sdz_premium'
  subscription_plan_id  TEXT,                     -- [HAR実証] 'sdz_premium_month'
  subscription_status   TEXT,                     -- [HAR実証] 'active'
  subscription_type     TEXT,                     -- [HAR実証] 'stripe'
  stripe_customer_id    TEXT,
  registered_date       TIMESTAMPTZ DEFAULT now(),
  created_at            TIMESTAMPTZ DEFAULT now(),
  updated_at            TIMESTAMPTZ DEFAULT now()
);
```
プラン別クォータ(`max_scheduled_tasks` 等 [HAR実証: 20])は `plans` テーブル or 設定で持ち、`webapi/me` 応答時に算出する。

### 2.2 platforms(マスタ / シード)
66サービスの静的定義。API仕様書の capability マトリクスをそのまま行に。
```sql
CREATE TABLE platforms (
  corename                 TEXT PRIMARY KEY,       -- [HAR実証] 'spotify','applemusicapp',...
  libelle                  TEXT NOT NULL,          -- 表示名 'Spotify'
  readable_playlist        BOOLEAN, writable_playlist BOOLEAN,
  readable_track           BOOLEAN, writable_track    BOOLEAN,
  readable_album           BOOLEAN, writable_album    BOOLEAN,
  readable_artist          BOOLEAN, writable_artist   BOOLEAN,
  writable_image           BOOLEAN,
  max_tracks_in_playlist   INT,                    -- [HAR実証] 250〜20000
  is_supporting_playlist_parts BOOLEAN DEFAULT false,
  time_per_track           NUMERIC,                -- [HAR実証] スロットル見積 0.03〜2.09
  action_type              TEXT,                   -- [HAR実証] 'play' | 'download'
  available                BOOLEAN DEFAULT true,   -- 終了サービスは false
  is_show                  BOOLEAN DEFAULT true
);
```

### 2.3 platform_connections(ユーザー×プラットフォームのOAuth接続)
```sql
CREATE TABLE platform_connections (
  id               BIGSERIAL PRIMARY KEY,
  user_id          BIGINT REFERENCES users(id),
  platform         TEXT REFERENCES platforms(corename),
  account_label    TEXT,                    -- [HAR実証] auth_path.account (接続先の表示名)
  is_premium       BOOLEAN,                 -- [HAR実証] auth_path.isPremium
  status           SMALLINT,                -- [HAR実証] auth_path.statut (1=connected)
  -- OAuthトークン(暗号化必須, §3.2)
  access_token     BYTEA,                   -- 保存時に暗号化
  refresh_token    BYTEA,
  token_expires_at TIMESTAMPTZ,
  scope            TEXT,
  extra            JSONB,                   -- サービス固有(Navidrome: base URL/salt, Apple: MusicKit token 等)
  created_at       TIMESTAMPTZ DEFAULT now(),
  UNIQUE(user_id, platform)
);
```

### 2.4 matchings(手動マッチングの学習ストア)← エンジンの記憶
```sql
CREATE TABLE matchings (
  id             BIGSERIAL PRIMARY KEY,    -- [HAR実証] 数値ID (例 542066)
  user_id        BIGINT REFERENCES users(id),
  type           SMALLINT NOT NULL,        -- [HAR実証] 1=track (album/artist用の別値を想定)
  -- source
  src_platform   TEXT, src_id TEXT, src_isrc TEXT, src_title TEXT, src_artist TEXT, src_album TEXT,
  -- destination
  dst_platform   TEXT, dst_id TEXT, dst_isrc TEXT, dst_title TEXT, dst_artist TEXT, dst_album TEXT,
  created_at     TIMESTAMPTZ DEFAULT now(),
  UNIQUE(user_id, src_platform, src_id, dst_platform)  -- 同一ソース→同一dst宛は1件
);
CREATE INDEX ON matchings (user_id, src_platform, src_id);  -- 解決時の高速引き
```
> [HAR実証] マッチングはユーザー単位で蓄積・ページング無しの全件返却。新規は配列先頭に積まれる。表記ゆれ(`M/A/R/R/S` vs `M|A|R|R|S`)はそのまま許容 → 正規化キーで引く(§4.3)。

### 2.5 batches(即時変換ジョブ)
```sql
CREATE TABLE batches (
  id            BIGSERIAL PRIMARY KEY,      -- [HAR実証] 例 2656840
  user_id       BIGINT REFERENCES users(id),
  destination   TEXT,                       -- [HAR実証] dest platform corename
  statut        SMALLINT,                   -- [HAR実証] 4=完了 (1=queued,2=running,3=error 等を想定)
  nb_playlists  INT DEFAULT 0, nb_albums INT DEFAULT 0,
  nb_artists    INT DEFAULT 0, nb_tracks INT DEFAULT 0,  -- [HAR実証] 種別ごと件数
  is_canceled   BOOLEAN DEFAULT false,
  created_date  TIMESTAMPTZ, start_date TIMESTAMPTZ, end_date TIMESTAMPTZ
);
```

### 2.6 scheduled_tasks + scheduled_task_history(cron同期)
```sql
CREATE TABLE scheduled_tasks (
  id             BIGSERIAL PRIMARY KEY,     -- [HAR実証] 例 2743500
  user_id        BIGINT REFERENCES users(id),
  title          TEXT,
  description    TEXT,
  method_id      SMALLINT,                  -- [HAR実証] 18=Replace in a Playlist (§6.2)
  -- 対象 (actionCode を分解して保持)
  src_platform   TEXT, src_id TEXT,         -- [HAR実証] actionCode.platformSrc / idSrc
  dst_platform   TEXT, dst_id TEXT,         -- [HAR実証] actionCode.platformDest / idDest ('newplaylist_soundiiz'で新規)
  -- スケジュール
  freq_type      SMALLINT,                  -- [HAR実証] 1,2,... (日次/週次 等)
  freq_interval  SMALLINT, freq_sub_interval SMALLINT,
  freq_hour      CHAR(4),                   -- [HAR実証] "HHMM" 文字列 例 "1309"
  timezone       TEXT,                      -- [HAR実証] "Europe/Paris"
  start_date     TIMESTAMPTZ,
  last_exec      TIMESTAMPTZ, next_exec TIMESTAMPTZ,  -- [HAR実証]
  is_active      BOOLEAN DEFAULT true,      -- [HAR実証] is_active
  is_running     BOOLEAN DEFAULT false,     -- [HAR実証] is_running (実行中ロック)
  created_date   TIMESTAMPTZ DEFAULT now(), updated_date TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE scheduled_task_history (
  id        BIGSERIAL PRIMARY KEY,          -- [HAR実証] 例 54971063
  task_id   BIGINT REFERENCES scheduled_tasks(id),
  start_date TIMESTAMPTZ, end_date TIMESTAMPTZ,  -- [HAR実証]
  statut    SMALLINT,                       -- [HAR実証] 2=成功
  error     TEXT,                           -- [HAR実証] null or メッセージ
  result    JSONB                           -- [HAR実証] 実行結果サマリ
);
```

### 2.7 ライブラリのキャッシュ(任意だが強く推奨)
[HAR実証] ライブラリ一覧はページング無しの全件ダンプ(Last.fm albums で5MB)。外部API直叩きは遅く不安定なので、`library_snapshots` テーブル or Redis に **プラットフォーム×種別のスナップショットをキャッシュ**し、TTLで再取得する設計にすると実用的。トラックは正規化スキーマ(API仕様書 §Track)でJSONB保存。

---

## 3. プラットフォーム連携層(最重要・最大工数)

### 3.1 共通アダプタインターフェース
全66サービスをこの契約に押し込む。capability が false のメソッドは `NotSupported` を返す。
```ts
interface PlatformAdapter {
  readonly corename: string;
  readonly capabilities: Capabilities;      // platforms行から
  readonly timePerTrack: number;            // スロットル用

  // OAuth
  getAuthUrl(state: string): string;
  handleCallback(code: string): Promise<TokenSet>;
  refresh(conn: Connection): Promise<TokenSet>;

  // READ (capability: readable_*)
  listPlaylists(conn): Promise<Playlist[]>;
  listPlaylistTracks(conn, playlistId): Promise<Track[]>;
  listFavoriteTracks(conn): Promise<Track[]>;
  listAlbums(conn): Promise<Album[]>;
  listArtists(conn): Promise<Artist[]>;
  searchTrack(conn, query: string): Promise<Track[]>;   // query = "title - artist" [HAR実証]

  // WRITE (capability: writable_*)
  createPlaylist(conn, {title, description, isPublic}): Promise<{id}>;
  addTracksToPlaylist(conn, playlistId, trackIds: string[]): Promise<void>;
  replacePlaylistTracks(conn, playlistId, trackIds: string[]): Promise<void>; // method 18
  addFavoriteTracks(conn, trackIds: string[]): Promise<void>;
  setPlaylistImage(conn, playlistId, imageDataUri): Promise<void>;            // capability: writable_image
  deletePlaylists(conn, playlistIds: string[]): Promise<void>;
}
```
アダプタの責務は **「外部APIの生レスポンス ↔ 正規化スキーマ(Track/Album/Artist/Playlist)の相互変換」**。上位(エンジン/ワーカー)は正規化スキーマしか知らない。

### 3.2 OAuthトークン管理
- **保存**: `platform_connections.access_token/refresh_token` を必ず**暗号化して**保存(KMS or アプリ側AES)。生保存は厳禁。
- **失効検知**: `token_expires_at` を見て事前リフレッシュ。リフレッシュ失敗時は `status` を落とし、`PLATFORM_DISCONNECTED` 通知([HAR実証: notifications種別])。
- **サービス別の癖**:
  - **Spotify** — 標準OAuth2 + refresh token。ISRC/UPC完備で最良のマッチ元。
  - **Apple Music** — MusicKit。developer token(署名JWT)+ user token の2層。書き込みはライブラリスコープ必須。
  - **YouTube Music** — 公式APIが弱く、非公式/内部エンドポイント併用が現実。`ytmusicapi` 相当の実装が要る。
  - **Navidrome / Subsonic / Jellyfin / Emby / Plex** — セルフホスト系。OAuthではなく **ベースURL + ユーザー資格情報**(Subsonic API: `u/t/s` トークン方式)。`extra` JSONBにサーバURLを持つ。[HAR実証: navidromeが接続済み]
  - **Last.fm** — スクロブル/お気に入り中心。書き込みはトラックのみ(capability `P-TtA-R-`)。
  - **Bandcamp** — 読み取り専用・playlist非対応(`--T-A-R-`)。
- **接続フロー**: `getAuthUrl` → ユーザーが外部で承認 → `handleCallback` でトークン交換 → `platform_connections` 作成。

### 3.3 レート制御(§7と連動)
各アダプタの `timePerTrack`([HAR実証])を使い、外部API呼び出しを**プラットフォーム単位でスロットル**。例: YouTube Music は 1.6秒/曲 → 直列 or 低並列でしか叩けない。ワーカーはトークンバケット等でこれを守る。

---

## 4. マッチング/解決エンジン(最大の差別化点)

**問題**: source側の曲(例: SpotifyのTrack)を、dest側(例: Navidrome)の**カタログ上の実トラックID**に対応付ける。ISRCが常にあるとは限らず、表記も揺れる。

### 4.1 解決パイプライン(1曲ごと)
```
resolve(srcTrack, dstPlatform, userId):
  1. 学習ヒット?    matchings から (user, src_platform, src_id, dst_platform) を引く
                    → あれば即 dst_id を返す(手動確定の記憶)          [HAR実証: matchings学習]
  2. ISRC一致       dstPlatform.searchByIsrc(srcTrack.isrc)             ← track解決の第一キー
                    (album は UPC, artist は name+genres)               [HAR実証: upc/name/genres]
  3. 厳密メタ一致   normalize(title)+normalize(artist) 完全一致
  4. ファジー一致   スコアリング(§4.2)で候補を順位付け、閾値超えを自動採用
  5. 未解決        isFound=false でマーク → UIで searchTrack 候補提示
                    → ユーザー選択 → POST /matching で matchings に保存(次回から手順1でヒット)
```

### 4.2 ファジースコアリング(推奨実装)
候補ごとに加重スコア:
```
score =  0.45 * sim(norm(title),  norm(cand.title))     // 文字列類似(Jaro-Winkler/トークンソート)
       + 0.35 * sim(norm(artist), norm(cand.artist))
       + 0.10 * albumBonus(album, cand.album)
       + 0.10 * durationBonus(|dur - cand.dur| <= 3s ? 1 : 0)   // [HAR実証: duration秒あり]
自動採用の閾値例: score >= 0.88。0.6〜0.88 は「候補あり(要確認)」、<0.6 は未発見。
```
- `sim` はトークンソート済みJaro-WinklerやレーベンシュタインベースのdiceでOK。
- `duration` は強力な補助シグナル(リマスター/別テイクの誤マッチ抑制)。

### 4.3 正規化ルール(表記ゆれ吸収)← 実装の勘所
[HAR実証] `M/A/R/R/S` vs `M|A|R|R|S`、`Trois Gymnopedies - First Movement` vs `Trois Gymnopedies (First Movement)` のような揺れが実データにある。`norm()` は最低限:
- Unicode NFKC正規化 + lowercase + アクセント除去(`Noé`→`noe`)
- 区切り文字統一(`/ | \ -` → 空白)、連続空白畳み込み
- ノイズ語の分離: `(feat. X)`, `- Remastered 2009`, `- Radio Edit`, `(UK 12" Remix)`, `[Mixed]` を抽出して**別フィールド**へ(本体一致とバージョン一致を分けて評価)
- `&` ↔ `and`、全角/半角統一
> ここを弱く作ると誤マッチ地獄。逆に厳しすぎると取りこぼす。**未解決を手動マッチングに落とし、それを学習**するループ(手順1↔5)が精度を担保する。

### 4.4 種別ごとのマッチキー [HAR実証]
| 種別 | 第一キー | フォールバック |
|---|---|---|
| track  | ISRC | title + artist (+duration) |
| album  | UPC  | title + artist (+nbTracks) |
| artist | name | name正規化 + genres補助 |

---

## 5. 変換フロー(即時バッチ)全体

`POST /webapi/playlist/convert` [HAR実証] を受けたときのワーカー処理:
```
1. batch 作成 (statut=queued) → batch_id 即返し
2. src プレイリスト/お気に入りのトラック配列を取得(既にリクエストに載っている場合はそれを使用)
3. for each track: resolve(track, dest, user)  ← §4、dest.timePerTrackでスロットル
4. dest.createPlaylist(...) → 新規 playlist id 取得
   ( idDest が既存指定ならそのIDへ、method=18なら replaceで全置換 )
5. dest.addTracksToPlaylist(found の dst_id[])   ← max_tracks_in_playlist を超えたら分割/切り詰め
6. batch を statut=完了(4) + nb_tracks 等を更新
7. 応答: { elements:[...isFound付], score, nbSource, id:新規destプレイリストID }   [HAR実証]
8. 通知: BATCH_END(ユーザーが購読していれば)
```
- `score/nbSource` = 解決できた曲数/総曲数([HAR実証] 7/7)。マッチ率としてUIに出す。
- 未解決分は結果画面で `tracks/search` → `matching` の手動フローへ([HAR実証] クエリは `"title - artist"`)。
- **べき等性**: ワーカーは中断・再実行を想定し、解決済みを再利用(matchings)して二重書き込みを避ける。

---

## 6. ジョブ & スケジューラ基盤

### 6.1 キュー
- 変換は**必ず非同期**([HAR実証] batchが即IDを返し後で完了)。Redis+BullMQ / SQS+worker などで実装。
- ジョブ種別: `convert_playlist`, `convert_library`(favorites/album/artist一括), `run_scheduled_task`。
- 進捗は `batches` / `scheduled_task_history` に書き、フロントは `current_batch_convert`([HAR実証: webapi/me])やバッチGETでポーリング。

### 6.2 同期モード(method_id)
[HAR実証] `method 18 = "Replace in a Playlist"`(dest全置換)。IDが飛んでいるので他モードが存在する設計。**[推論]** 実装すべき代表モード:
| method_id(推定) | 挙動 |
|---|---|
| (append系) | dest末尾に**追記**(重複はスキップ) |
| 18 [実証] | dest内容を**全置換**(src=真実) |
| (mirror系) | src削除もdestに反映する完全ミラー |
| (favorites系) | プレイリストでなくお気に入り(track/album/artist)を同期 |
自前実装ではまず「追記」「全置換」の2つを用意すれば実用。

### 6.3 スケジューラ
- cron(1分粒度)で `scheduled_tasks WHERE is_active AND next_exec <= now() AND NOT is_running` を拾ってキュー投入。
- 実行時: `is_running=true` ロック → 変換 → `scheduled_task_history` 追記 → `last_exec`更新 → `next_exec` を `freq_type/interval/hour/timezone` から再計算 → `is_running=false`。
- [HAR実証] **空ボディ `POST /api/scheduledtasks/{id}` = 即時実行トリガー**(next_execを待たず今すぐキュー投入)。
- `freq_hour` は `"HHMM"` 文字列、`timezone` 込みで次回時刻を計算(DST注意)。
- クォータ: 作成時に `count(scheduled_tasks) < max_scheduled_tasks`([HAR実証: 20])を検査。

---

## 7. レート制御・クォータ・上限

| 制約 | 出所 | 実装 |
|---|---|---|
| `time_per_track`(0.03〜2.09秒/曲) | [HAR実証] platform行 | ワーカーが外部API呼び出しをプラットフォーム単位でスロットル。ETA表示にも使用 |
| `max_tracks_in_playlist`(250〜20000) | [HAR実証] | 書き込み前に切り詰め or 分割。超過はUI警告 |
| `max_scheduled_tasks`(プラン別, 例20) | [HAR実証] | タスク作成時に検査 |
| capability(読み書き×種別) | [HAR実証] マトリクス | 変換元/先の選択肢バリデーション。false操作は事前に弾く |
| 外部APIのレート制限 | 各サービス規約 | アダプタ内でリトライ/バックオフ。429時は指数バックオフ |
| ページング無し全件取得 | [HAR実証] | 大規模ライブラリ(Last.fm 10000件)対応。キャッシュ+ストリーミング応答を検討 |

---

## 8. 認証・セッション・課金

### 8.1 認証 [HAR実証]
- リクエストに `Authorization` ヘッダ無し → **セッションCookie方式**。
- 状態変更(convert/delete/matching/scheduledtask)は **`X-CSRF-Token` 必須**。
- `webapi/me` が `auth_tokens.access_token`(`expires_in`付き)を発行 → モバイル/外部API用のBearer。
- **[推論] クローンでの推奨**: セッション(Web) or JWT(API)いずれか。状態変更にCSRF or ダブルサブミットクッキー。プラットフォームのOAuthトークンとユーザー認証は完全に別物として扱う。

### 8.2 課金(Stripe)[HAR実証: subscription_type='stripe', m.stripe.comへの通信]
- Stripe Customer/Subscription をユーザーに紐付け。プラン(`sdz_premium_month` 等)でクォータを決定。
- Webhook(`checkout.session.completed`, `customer.subscription.updated/deleted`)で `subscription_status` を同期。
- 無料/有料でmaxを分岐(接続数・スケジュールタスク数・一括変換上限など)。

### 8.3 通知 [HAR実証: notifications配列]
`BATCH_END` / `TASK_ERROR` / `PLATFORM_DISCONNECTED` をユーザーが購読。ジョブ完了・失敗・トークン失効時にメール/アプリ内通知を発火。

---

## 9. 監視・運用 [HAR実証]
- **Sentry** — フロント/バックのエラー追跡(HARで JS SDK 9.47.1、release ハッシュでデプロイ識別)。ワーカーの失敗は必ずSentry+`scheduled_task_history.error`の両方に残す。
- **Zendesk** — サポート導線(任意)。
- ジョブ失敗の再試行、トークン失効の一括検知、外部API障害時のサーキットブレーカを運用に組み込む。

---

## 10. 構築順序(MVPマイルストーン)

1. **M1 コア骨格**: users/auth、platforms シード投入、正規化スキーマ(Track/Album/Artist/Playlist)の型定義。
2. **M2 最初の2アダプタ**: Spotify(OAuth完備・ISRC良好)+ Navidrome/Subsonic(セルフホスト)。read/write両対応。
3. **M3 マッチングエンジン**: §4のパイプライン(ISRC→厳密→ファジー→未解決)。正規化ルール(§4.3)。
4. **M4 即時変換**: `playlist/convert` 相当を非同期ジョブで。結果に isFound/score。手動マッチング+学習ループ。
5. **M5 スケジュール同期**: scheduled_tasks + cron + method 18(全置換)/追記。即時実行トリガー。
6. **M6 横展開**: Apple Music、YouTube Music、Last.fm… とアダプタを増やす(コアは変えずアダプタだけ追加できる設計の検証)。
7. **M7 課金・クォータ・通知**: Stripe、プラン別上限、通知3種。

> **一番効くアドバイス**: アダプタを増やす前に **M2の2サービスでエンジン(§4)と変換フロー(§5)を完全に固める**こと。エンジンが正規化スキーマだけに依存していれば、以降のアダプタ追加は純粋な足し算になる。ここを最初に正しく分離できるかがクローンの成否を分ける。

---
※ 本書のDBスキーマ・アルゴリズム・マイルストーンは、HARで観測できたAPI挙動([HAR実証]印)から逆算した実装提案です。内部実装はHARに現れないため [推論] 部分は自由に設計変更して構いません。機密情報(トークン・個人ライブラリの中身)は一切含めていません。
