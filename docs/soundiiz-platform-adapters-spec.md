# Soundiiz プラットフォーム連携リファレンス(全66サービス)

`soundiiz-backend-implementation-spec.md` §3(連携層)の詳細版。**Soundiizが対応する66サービス全てのアダプタ実装ガイド**。
各サービスの認証方式・API種別・ISRC/UPC有無・capability・実装上の癖を網羅する。

> **凡例**
> - **caps** = `PpTtAaRr` 8ビット([HAR実証])。大文字位置=読(P/T/A/R)、小文字位置=書(p/t/a/r)。`1`=対応 `0`=非対応。順に playlist / track / album / artist。
> - **max** = 1プレイリスト最大曲数 [HAR実証]、**tpt** = time_per_track 秒 [HAR実証](外部APIスロットルの目安)。
> - **信頼度**: ✅ 公式API・仕様安定 / ⚠️ 非公式・リバースエンジニアリング要(実装前に現行仕様の検証必須)/ 🪦 サービス終了・レガシー(capability全0、枠のみ)。
> - 外部サービスの内部API仕様は変わりうる。⚠️印は必ず最新を確認すること。ここは「どのパターンで攻めるか」の地図。

---

## 0. 認証パターン別サマリ(実装の軸)

アダプタのコード構造は認証方式でほぼ決まる。66サービスは以下の8パターンに収まる:

| パターン | 代表サービス | アダプタ実装の要点 |
|---|---|---|
| **A. OAuth2 Authorization Code** | Spotify, Deezer, SoundCloud, Amazon Music, Yandex, Audiomack, Audius | 標準。`getAuthUrl`→callback→token交換→refresh。最も素直 |
| **B. OAuth2 + 署名JWT(developer token)** | Apple Music (MusicKit) | 2層トークン。開発者JWT(ES256署名)+ ユーザートークン |
| **C. Google OAuth + 非公式** | YouTube Music, YouTube | 公式Data APIは弱い。ブラウザ認証ヘッダ/内部APIを併用 |
| **D. ユーザー資格情報 → トークン** | Subsonic系(Navidrome/Jellyfin/Emby/Plex), Tidal, Qobuz, Napster | ID/PW or サーバURL+資格情報でセッション/トークン取得 |
| **E. APIキーのみ / 公開** | MusicBrainz, Discogs, Last.fm(読), Setlist.fm, Jamendo, ListenBrainz | 認証簡易。書き込みは別途トークン(Last.fm/ListenBrainz) |
| **F. 非公式セッショントークン(要リバース)** | JioSaavn, Zvuk, VK, Anghami, Boomplay, Resso, JOOX, KKBOX, Claro, Movistar 等 | 公式APIなし。内部エンドポイントを解析。⚠️壊れやすい |
| **G. 読み取り専用 / スクレイプ** | Bandcamp, Hype Machine, Reddit, iTunes, 8Tracks, Musi | 書き込み不可。公開データ取得のみ |
| **H. 終了・レガシー** | Grooveshark, Rdio, Fanburst, Google Play Music, Groove | 🪦 実装不要。UI枠のみ or 完全撤去 |

---

## 1. マスターテーブル(全66・HAR実証値)

| corename | 表示名 | caps `PpTtAaRr` | max | tpt | 認証 | 信頼度 |
|---|---|---|--:|--:|:--:|:--:|
| soundiiz | Soundiiz(自社) | 11111111 | 20000 | 0.03 | 内部 | ✅ |
| spotify | Spotify | 11111111 | 10000 | 0.80 | A | ✅ |
| applemusicapp | Apple Music | 11111111 | 10000 | 0.44 | B | ✅ |
| ytmusic | YouTube Music | 11111111 | 5000 | 1.60 | C | ⚠️ |
| youtube | YouTube | 11110011 | 5000 | 1.78 | C | ✅/⚠️ |
| amazonmusic | Amazon Music | 11111111 | 2500 | 1.38 | A | ⚠️ |
| deezer | Deezer | 11111111 | 5000 | 0.25 | A | ✅ |
| tidal | TIDAL | 11111111 | 10000 | 0.19 | D/A | ✅ |
| qobuz | Qobuz | 11111111 | 1999 | 0.11 | D | ✅ |
| soundcloud | SoundCloud | 11111111 | 500 | 0.41 | A | ✅ |
| lastfm | Last.fm | 10111010 | 10000 | 0.07 | E | ✅ |
| navidrome | Navidrome | 11111111 | 10000 | 0.51 | D(Subsonic) | ✅ |
| subsonic | Subsonic | 11111111 | 10000 | 0.31 | D(Subsonic) | ✅ |
| jellyfin | Jellyfin | 11111111 | 10000 | 0.31 | D | ✅ |
| emby | Emby | 11111111 | 10000 | 0.12 | D | ✅ |
| plex | Plex | 11001010 | 10000 | 1.18 | D | ✅ |
| bandcamp | Bandcamp | 00101010 | 10000 | 0.00 | G | ⚠️ |
| tidal | (上記) | | | | | |
| yandexmusic | Yandex Music | 11111111 | 10000 | 0.54 | A/F | ⚠️ |
| zvooq | Zvuk(旧SberZvuk) | 11111111 | 10000 | 0.42 | F | ⚠️ |
| vk | VKontakte | 11111000 | 1000 | 0.97 | F | ⚠️ |
| jiosaavn | JioSaavn | 11111111 | 10000 | 1.09 | F | ⚠️ |
| kkbox | KKBOX | 11111000 | 5000 | 0.36 | F | ⚠️ |
| anghami | Anghami | 10000000 | 10000 | 0.87 | F(読のみ) | ⚠️ |
| boomplay | Boomplay | 11111100 | 10000 | 0.44 | F | ⚠️ |
| resso | Resso | 11111111 | 10000 | 0.30 | F | ⚠️🪦 |
| joox | JOOX | 11110000 | 10000 | 0.77 | F | ⚠️ |
| napster | Napster | 11111111 | 10000 | 0.77 | D | ✅ |
| iheartradio | iHeartRadio | 11111010 | 5000 | 0.45 | A | ⚠️ |
| pandora | Pandora | 11111100 | 5000 | 0.30 | F | ⚠️ |
| audiomack | Audiomack | 11111111 | 2000 | 0.18 | A | ✅ |
| audius | Audius | 11111111 | 5000 | 0.34 | A(分散) | ✅ |
| jamendo | Jamendo | 10111010 | 10000 | 0.00 | E | ✅ |
| beatport | Beatport | 11100011 | 10000 | 1.88 | A | ✅ |
| beatsource | Beatsource | 11000000 | 10000 | 0.85 | A | ⚠️ |
| idagio | IDAGIO | 11111111 | 10000 | 0.39 | F | ⚠️ |
| moodagent | Moodagent | 11111111 | 10000 | 0.00 | F | ⚠️ |
| qub | QUB musique | 11111111 | 1000 | 0.13 | F | ⚠️ |
| dmusic | d'Music | 11111111 | 10000 | 0.27 | F | ⚠️ |
| xploremusic | Xplore Music | 11111111 | 10000 | 0.00 | F | ⚠️ |
| brisamusic | Brisamusic | 11111111 | 10000 | 0.28 | F | ⚠️ |
| claromusica | Claro Música | 11111111 | 10000 | 1.43 | F | ⚠️ |
| movistar | Movistar Música | 11111111 | 10000 | 0.30 | F | ⚠️ |
| yousee | YouSee Musik | 11111111 | 1000 | 0.21 | F | ⚠️ |
| telmore | Telmore Musik | 11111110 | 1000 | 0.46 | F | ⚠️ |
| playzer | Playzer | 11110010 | 10000 | 0.38 | F | ⚠️ |
| wynk | Wynk Music | 11000000 | 10000 | 0.00 | F | ⚠️ |
| slackerradio | LiveOne(旧Slacker) | 11111111 | 500 | 2.09 | F | ⚠️ |
| hearthis | hearthis.at | 11110000 | 10000 | 0.34 | A | ✅ |
| dailymotion | Dailymotion | 11000011 | 250 | 0.42 | A | ✅ |
| musicbrainz | MusicBrainz | 10001010 | 10000 | 0.34 | E | ✅ |
| listenbrainz | ListenBrainz | 11111010 | 10000 | 0.90 | E | ✅ |
| discogs | Discogs | 10001000 | 10000 | 0.00 | E | ✅ |
| setlist | Setlist.fm | 10000000 | 10000 | 0.00 | E(読) | ✅ |
| hypem | Hype Machine | 10110000 | 10000 | 0.07 | G | ⚠️ |
| reddit | Reddit | 10000000 | 10000 | 0.00 | G/A | ✅ |
| eighttracks | 8Tracks | 10100000 | 10000 | 0.00 | G | 🪦 |
| musi | Musi | 10100000 | 10000 | 0.00 | G | ⚠️ |
| itunes | iTunes | 10000000 | 10000 | 0.00 | G(ローカル) | ✅ |
| sdigital | 7digital | 10101010 | 10000 | 0.00 | A | ✅ |
| xboxmusic | Groove(旧Xbox) | 11111010 | 10000 | 0.00 | H | 🪦 |
| googlemusic | Google Play Music | 10101010 | 1000 | 0.00 | H | 🪦 |
| grooveshark | Grooveshark | 00000000 | 10000 | 0.00 | H | 🪦 |
| rdio | Rdio | 00000000 | 10000 | 0.00 | H | 🪦 |
| fanburst | Fanburst | 00000000 | 10000 | 0.00 | H | 🪦 |
| pulselocker | Pulselocker | 11111111 | 10000 | 0.00 | H | 🪦 |
| soundmachine | SoundMachine | 11000000 | 10000 | 0.02 | F(B2B) | ⚠️ |

> ⚠️ soundmachine/brisamusic/movistar/claro等は地域限定B2B/telco系。実装優先度は低い。

---

## 2. Tier 1 — 主要ストリーミング(公式API・最優先実装)

### spotify ✅ — パターンA
- **認証**: OAuth 2.0 Authorization Code + PKCE。refresh token あり。
- **API**: Web API `https://api.spotify.com/v1`。playlist/track/album/artist 全対応、ライブラリ・お気に入り(saved)も完備。
- **ISRC/UPC**: **完備**(`external_ids.isrc` / album `external_ids.upc`)。**最良のマッチ元**。
- **書き込み**: playlist作成・トラック追加(URI指定)・並べ替え・画像設定(base64 JPEG, [HAR実証: writable_image])。
- **スコープ**: `playlist-read-private playlist-modify-private/public user-library-read/modify`。
- **癖**: レート制限は動的(429 + Retry-After)。ローカルファイルは `spotify:track:local...` の擬似ID([HAR実証: matchingsに出現])で検索不可 → 手動マッチ行き。
- **lib**: spotify-web-api-node / spotipy。

### applemusicapp ✅ — パターンB(MusicKit)
- **認証**: **2層**。①開発者トークン=Team ID/Key IDで**ES256署名したJWT**(最大6ヶ月)。②ユーザートークン=MusicKit JS/デバイスで取得(`Music-User-Token` ヘッダ)。
- **API**: Apple Music API `https://api.music.apple.com/v1`。`/me/library/*` でユーザーライブラリ読み書き。
- **ISRC/UPC**: カタログ側はISRCあり。ライブラリ項目はストアIDベース。
- **書き込み**: `/me/library/playlists` 作成、トラック追加。カタログID or ライブラリIDで指定。
- **癖**: ユーザートークンはブラウザ/デバイス取得が前提(サーバ単独では発行不可)→ 接続フローにMusicKit JSを挟む。地域(storefront)で結果が変わる。[HAR実証: playlistLinkに `geo.music.apple.com`, storefront `ja-JP`]
- **lib**: apple-music-api系。MusicKit JS。

### ytmusic ⚠️ — パターンC(非公式)
- **認証**: Googleアカウント。公式YouTube Data API v3ではプレイリスト操作は可能だが**Music固有のライブラリ/おすすめは非対応** → 実運用は**ブラウザ認証ヘッダ(cookie/`SAPISIDHASH`)を使った内部API**。
- **API**: 内部 `music.youtube.com/youtubei/v1/*`。`ytmusicapi`(Python)が事実上の標準実装。
- **ISRC**: **無し**(動画ベース)→ title+artist+durationマッチが主。誤マッチ多め、正規化(§4.3)が特に重要。
- **書き込み**: playlist作成・トラック追加可(内部API)。tpt=1.60と遅い→スロットル必須。
- **癖**: 認証が壊れやすい(cookie失効)。公式APIに寄せると機能が削れる。実装難度高。

### youtube ✅/⚠️ — パターンC
- **認証**: Google OAuth2 + YouTube Data API v3(公式)。
- **API**: `https://www.googleapis.com/youtube/v3`。playlist読み書き(`playlistItems.insert`)対応。
- **caps**: `11110011`(album非対応、artist読み書き=チャンネル扱い)。[HAR実証: track add例が `music.youtube.com/watch?v=...`]
- **ISRC**: 無し。動画ID(`videoId`)がネイティブID。
- **癖**: APIクォータ厳しい(1日10000ユニット、検索100/回)。tpt=1.78。

### amazonmusic ⚠️ — パターンA(非公式寄り)
- **認証**: Amazonアカウント(Login with Amazon)。公式の一般向けPlaylist書き込みAPIが乏しく、内部API利用が現実。
- **ISRC**: カタログにあり。
- **癖**: [HAR実証] playlist ID はUUID(`83b87a5c-70f8-...`)、`music.amazon.de` 地域別。max=2500と低め。tpt=1.38。地域(marketplace)依存。

### deezer ✅ — パターンA
- **認証**: OAuth2。`https://api.deezer.com`。
- **ISRC/UPC**: **完備**(track.isrc / album.upc)。Spotifyと並ぶ良マッチ元。
- **書き込み**: playlist作成・トラック追加(`/playlist/{id}/tracks`)。favorites対応。
- **癖**: 素直で実装しやすい。max=5000。

### tidal ✅ — パターンD/A
- **認証**: OAuth2(新)。旧来はユーザー資格。`https://openapi.tidal.com` / 内部 `api.tidal.com/v1`。
- **ISRC/UPC**: 完備。ハイレゾメタ充実。
- **書き込み**: playlist作成・トラック追加。max=10000, tpt=0.19(速い)。

### qobuz ✅ — パターンD
- **認証**: app_id/secret + ユーザーlogin → user_auth_token。`https://www.qobuz.com/api.json/0.2`。
- **ISRC/UPC**: 完備(ハイレゾ・クラシック強い)。max=1999(半端な上限[HAR実証])。
- **書き込み**: playlist操作対応。

### soundcloud ✅ — パターンA
- **認証**: OAuth 2.1(Authorization Code + PKCE)。`https://api.soundcloud.com`。
- **ISRC**: 一部のみ(UGCなので欠損多い)→ title+artistマッチ主。
- **癖**: **max=500と極端に低い**[HAR実証]。API申請が絞られている時期あり。track/playlist(set)対応。

---

## 3. Tier 2 — セルフホスト / オープン(Subsonic系)✅ パターンD

**共通**: OAuthではなく **サーバURL + ユーザー資格情報**。`platform_connections.extra` にベースURLを持つ([実装仕様書 §3.2])。ISRCは基本無し → メタデータ一致が主。ローカルライブラリなので「カタログ検索」= 自分のサーバ内検索。

### navidrome / subsonic ✅
- **認証**: **Subsonic API**。`GET .../rest/ping.view?u={user}&t={md5(password+salt)}&s={salt}&v=1.16.1&c={client}&f=json`(トークン方式、[HAR実証: `getCoverArt.view?u=...&t=...&s=...&c=soundiiz`])。
- **API**: `/rest/getPlaylists`, `/getPlaylist`, `/createPlaylist`, `/updatePlaylist`, `/search3`, `/star`(お気に入り)。
- **ISRC**: 無し(タグ依存)。曲IDはサーバ内部ID。
- **癖**: Navidromeは書き込み全対応(`11111111`)。[HAR実証で接続済み・330 playlists]。`c=soundiiz` のようにクライアント名を送る。
- **lib**: subsonic-api クライアント各種。**Navidrome/Airsonic/Gonic 等 Subsonic互換は同一アダプタで吸収可能**。

### jellyfin / emby ✅
- **認証**: `X-Emby-Authorization` ヘッダ + `/Users/AuthenticateByName`(user/pass)→ AccessToken。サーバURL必須。
- **API**: `/Items?IncludeItemTypes=Audio`, `/Playlists`(作成), `/Playlists/{id}/Items`(追加)。
- **癖**: JellyfinはEmbyのfork、APIほぼ共通 → 共通アダプタ+差分。全対応(`11111111`)。

### plex ✅
- **認証**: Plex token(`X-Plex-Token`)。plex.tv経由でPIN認証 or 直接token。サーバURL(PMS)必須。
- **caps**: `11001010`([HAR実証] track書き込み非対応・album/artist読みのみ)。playlist(audio)中心。
- **API**: `/playlists`, `/library/sections`。tpt=1.18。

---

## 4. Tier 3 — スクロブル / メタデータ / ディスカバリー ✅ パターンE/G

### lastfm ✅ — パターンE
- **認証**: API key + secret。書き込み(スクロブル/お気に入り)は Web Auth の session key。
- **caps**: `10111010`([HAR実証] 書き込みはトラックのみ、album/artist/playlistは読みか非対応)。[HAR実証で接続済み・albums 10000件]
- **ISRC**: 無し。MBID(MusicBrainz ID)を持つことがある。
- **用途**: お気に入り/スクロブル履歴のエクスポート元として強力。tpt=0.07(速い)。

### listenbrainz ✅ — パターンE
- **認証**: user token(ヘッダ)。`https://api.listenbrainz.org`。
- **caps**: `11111010`。MusicBrainz連携でメタ品質高い。オープンなLast.fm代替。

### musicbrainz ✅ — パターンE
- **認証**: 不要(読み取り)。書き込みはアカウント。`https://musicbrainz.org/ws/2`。
- **caps**: `10001010`(読み取り+album/artist、playlistは非対応)。**マッチングの権威データソース**(ISRC↔MBID解決に使える補助DB)。
- **癖**: レート厳格(1req/秒、User-Agent必須)。書き込みより**照合用リファレンス**として使うのが賢い。

### discogs ✅ — パターンE
- **認証**: token or OAuth。`https://api.discogs.com`。
- **caps**: `10001000`(読み+album)。physical/releaseメタが強い。action=`buy`(購入導線)。

### setlist ✅ — パターンE(読のみ)
- **認証**: API key。`https://api.setlist.fm/rest/1.0`。
- **caps**: `10000000`(playlist読みのみ)。ライブのセットリスト→プレイリスト化。

### hypem ⚠️ / reddit ✅ / musi ⚠️ / eighttracks 🪦 — パターンG
- **hypem**(Hype Machine): `10110000`。ブログ集約チャート。公開JSON(`/playlist/*/json`)スクレイプ。[HAR実証で track add に使用]
- **reddit**: `10000000`。OAuth2 or 公開JSON。サブレディットの投稿から曲抽出。
- **musi**: `10100000`。iOS向け、非公式。
- **eighttracks**: 🪦 2019サービス終了。実装不要。

### itunes ✅ — パターンG(ローカル)
- **caps**: `10000000`。action=`download`。ローカルの iTunes/Music.app ライブラリXML(`iTunes Music Library.xml`)or Search API。DRMなしのローカル読み取り。

---

## 5. Tier 4 — 地域ストリーミング(多くが非公式)⚠️ パターンF

**共通の攻め方**: 公式APIが無い/限定的 → モバイルアプリor Webの内部エンドポイントを解析。セッショントークン取得 → JSON API。**壊れやすいので抽象化を薄く保ち、失敗時は該当サービスだけ無効化**できる設計に。

- **yandexmusic**(Yandex Music) ⚠️: `11111111`。`yandex-music` (Python)等の非公式lib成熟。OAuthトークン方式。ISRCあり。
- **zvooq**(Zvuk) ⚠️: `11111111`。旧SberZvuk。内部API、ロシア地域。
- **vk**(VKontakte) ⚠️: `11111000`(album書き込み以降なし)。VK Audio内部API、規約グレー。max=1000。
- **jiosaavn** ⚠️: `11111111`。インド。非公式API(`saavn.me`系ラッパ)。tpt=1.09。
- **kkbox** ⚠️: `11111000`。台湾/東南アジア。**公式Open API存在**(OAuth2 client credentials)だが書き込みは限定。
- **anghami** ⚠️: `10000000`(**読み取りのみ**)。中東。エクスポート元専用。
- **boomplay** ⚠️: `11111100`。アフリカ最大手。内部API。
- **resso** ⚠️🪦: `11111111`。ByteDance、多くの地域で**終了済み** → 実質レガシー。
- **joox** ⚠️: `11110000`(track書き込みまで、album/artist非対応)。Tencent、東南アジア。
- **idagio** ⚠️: `11111111`。クラシック専門。作品/楽章単位の特殊メタ。
- **moodagent** ⚠️: `11111111`。デンマーク、ムードベース。
- **napster** ✅: `11111111`。旧Rhapsody。公式API(`api.napster.com`, apikey+OAuth)。
- **iheartradio** ⚠️: `11111010`。米ラジオ。内部API。
- **pandora** ⚠️: `11111100`。米国のみ。非公式API有名だが不安定。
- **audiomack** ✅: `11111111`。公式OAuth2 API(`https://api.audiomack.com`)。HipHop/Afro強い。
- **audius** ✅: `11111111`。**分散型**(ディスカバリーノード群)。公式API(`https://api.audius.co` → node選択)。ウォレット/署名。オープンで実装しやすい。
- **jamendo** ✅: `10111010`。CC音楽。公式API(client_id)。
- **beatport** ✅: `11100011`。DJ/EDM。OAuth2公式(`https://api.beatport.com`)。BPM/key メタ有用。tpt=1.88(遅い)。
- **beatsource** ⚠️: `11000000`(playlist読み書きのみ)。Beatport系列、DJ向け。
- **qub / dmusic / xploremusic / brisamusic / claromusica / movistar / yousee / telmore / playzer / wynk / slackerradio(LiveOne)** ⚠️: いずれもtelco/地域限定。内部API方式(パターンF)。**実装優先度は最低**。必要になった時に個別対応でよい。

---

## 6. Tier 5 — ダウンロード/購入・動画・その他

- **bandcamp** ⚠️ — パターンG: `00101010`(**読み取り専用**、playlist非対応、track/album/artist読み)。action=`download`。公式APIは限定的、fan collectionは公開JSONから。[HAR実証で接続済み]。**エクスポート元専用**。
- **sdigital**(7digital) ✅ — パターンA: `10101010`(読み+track/album/artist)。公式API(OAuth)。購入型。
- **hearthis**(hearthis.at) ✅ — パターンA: `11110000`。SoundCloud類似のUGC。公式API。
- **dailymotion** ✅ — パターンA: `11000011`。動画。max=**250**([HAR実証]最小)。playlist+artist(チャンネル)。
- **traxsource** — download系(HAR platform/listには居るがlinkのみ)。DJハウス専門。
- **grooveshark / rdio / fanburst / pulselocker / googlemusic(Google Play Music) / xboxmusic(Groove)** 🪦 — パターンH: **全てサービス終了**。caps全0 or レガシー。**実装不要**、UIに枠を残すかどうかだけの判断。Google Play Musicは2020終了(YT Musicへ移行済み)。
- **soundmachine** ⚠️: `11000000`。B2B店舗BGM。特殊、優先度最低。

---

## 7. 実装戦略(66サービスをどう捌くか)

1. **アダプタは認証パターン単位で基底クラス化**(§0の A〜H)。Subsonic系4サービス、OAuth2系多数を**基底の共通化**で圧縮できる。66個を個別に書くのではなく、**8パターン × 差分**。
2. **ISRC/UPCの有無でマッチ品質が二分**する:
   - **有り(Spotify/Deezer/Tidal/Qobuz/Apple/Yandex等)** → ISRC直マッチで高精度。
   - **無し(YouTube Music/Subsonic系/SoundCloud/Last.fm/UGC全般)** → 正規化+ファジー([実装仕様書 §4.3])が生命線。ここに一番投資する。
3. **tptでスロットル階層を持つ**: 速い(soundiiz0.03〜qobuz0.11〜tidal0.19)は並列可、遅い(slacker2.09/beatport1.88/youtube1.78/ytmusic1.60/amazon1.38/claro1.43)は直列+バックオフ。
4. **⚠️非公式サービスは「疎結合・単独無効化可能」に**。1つのサービスのAPI変更が全体を落とさないよう、アダプタ単位でサーキットブレーカ+`available`フラグ動的切り替え。
5. **実装順(投資対効果)**:
   - **第1波**: spotify, deezer, applemusicapp, tidal, qobuz(ISRC完備・公式・ユーザー多い)+ navidrome/subsonic(セルフホスト需要)。
   - **第2波**: ytmusic, youtube, amazonmusic, soundcloud, lastfm(需要大だが非公式/ISRC無しで難度高)+ jellyfin/emby/plex。
   - **第3波**: napster, audiomack, audius, beatport, deezer系公式、yandex/jiosaavn等の主要地域。
   - **やらない/後回し**: 🪦全部、telco/B2B系(qub/claro/movistar/wynk/soundmachine…)、地域超ニッチ。
6. **メタデータDBを補助に**: MusicBrainz/ListenBrainzを**照合リファレンス**として使い、ISRC無しサービス間のマッチを底上げできる(ISRC↔MBID↔title/artistの橋渡し)。

---

## 8. まとめ

- **66サービス = 8認証パターンの組み合わせ**。個別実装ではなくパターン基底+差分で作る。
- **公式API・ISRC完備の一軍(Tier1〜2の10数個)を完璧にすれば、Soundiizの体験の8割**はカバーできる。
- **残りの⚠️非公式・地域サービスは疎結合で足していく**もの。全部を最初に作る必要はない。
- **マッチングエンジン([実装仕様書 §4])がプラットフォーム非依存**であることが、この横展開を可能にする最大の前提。

---
※ 外部サービスの内部API仕様・認証方式は本書執筆時点の一般的理解に基づく。⚠️印(非公式/リバースエンジニアリング)は実装前に必ず現行の仕様・利用規約を確認すること。caps/max/tpt値はHAR実証。機密情報は含まない。
