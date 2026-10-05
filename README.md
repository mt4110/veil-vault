# veil-vault

Cloudflare Workers + D1で動く、一回取得型の暗号文共有APIの初期実装です。
クライアントが暗号化したUTF-8文字列を保存し、取得時に単一の
`DELETE … RETURNING`文でD1の現在のテーブルからレコードを削除します。
同じレコードの暗号文を取得できるリクエストは最大1つです。

## API

| エンドポイント | 入力 | 成功時 |
| --- | --- | --- |
| `POST /api/secrets` | `Authorization: Bearer …`、`Content-Type: text/plain`、暗号文 | `201`、`{"id":"UUID-v4","expires_in_seconds":86400}` |
| `GET /api/secrets/:id` | UUID v4 | `200`、保存した暗号文（`text/plain; charset=utf-8`） |

暗号文は空白だけではないUTF-8文字列とし、最大64 KiB（65,536バイト）です。
この上限は初期実装のメモリ・保存量を抑えるための運用上の選択です。
暗号方式、nonce、認証タグなどの形式はクライアント側で決めます。
サーバーは受け取った文字列が暗号文かどうかを検証できません。
実際の秘密情報や復号キーをPOSTしてはいけません。

- 存在しない・取得済み・期限切れのIDは、すべて同じ`404`です。
- 不正なID・UTF-8・空の入力は`400`、サイズ超過は`413`、異なるメディア型は`415`です。
- 対応外のメソッドは`405`です。`HEAD`と`OPTIONS`では消費しません。
- D1の操作失敗は内部情報を含まない`500`です。取得処理の自動再試行はしません。
- POSTの認証なし・不正は`401`です。送信認証トークンは復号キーとは別に管理します。
- `SERVICE_ENABLED`が`true`以外または未設定ならAPIは`503`です。
  POSTはWorker secretの`PUBLISH_TOKEN`が未設定・形式不正でも`503`になります。
- すべてのAPI応答に`Cache-Control: no-store`を付け、Worker Cache APIは使用しません。

`GET`には消費という副作用があります。リンクプレビュー、セキュリティスキャナー、
ブラウザーの先読みがGETすると、そのリクエストが消費します。
取得APIのURLをそのままSlackなどに貼らず、クライアントで明示的に取得してください。
確認画面やブラウザー向けの共有ページは今回の実装に含みません。

## 期限と取得保証

`created_at`はD1が生成するUTCのUnix秒です（SQLite上の宣言型は`TIMESTAMP`）。
24時間の境界をSQL内のD1時刻で判定し、期限切れのレコードは削除しても暗号文を返しません。
時刻の精度は秒単位です。
未取得の期限切れレコードは毎時のCronでも削除します。
Cronの遅延・失敗時も取得期限は有効ですが、レコードの保管は掃除の成功まで延びます。
Cron失敗時は秘密情報を含まない固定メッセージをログに出します。

削除が完了してから応答するため、削除後の通信切断ではデータが失われることがあります。
受信者への確実な一回配信や、復号の成功は保証しません。
POSTの応答が失われた場合も、クライアントが再送すると別のレコードができます。

D1の削除は、現在のテーブルからの論理削除です。物理媒体、レプリカ、バックアップ、
Time Travelの履歴からの完全消去は保証しません。
管理者が削除前のDBへ復元すると、未期限切れの取得済みデータが復活し、
同じIDで再取得され得ます。Read-Onceの保証はDB復元・管理者による再挿入を含みません。
[D1のTime Travel仕様](https://developers.cloudflare.com/d1/reference/time-travel/)を参照してください。

## E2EEとの接続

```text
送信クライアント: 平文 + 復号キー → 認証付き暗号化
                         │ 暗号文だけをPOST
                         ▼
                 veil-vault / D1
                         │ GETでアトミックに削除・取得
                         ▼
受信クライアント: 暗号文 + 別途共有された復号キー → 復号
```

共有リンクを設計する場合、復号キーは`#key=...`のようなURLフラグメントに置き、
送信前にクライアントが分離します。HTTPではフラグメントがサーバーへ送られません。
CLIでもURL全体を本文・ヘッダー・ログへ転記しないことが必要です。
このAPI自体はリンク生成、鍵生成、暗号化・復号、`veil-env`との接続を実装しません。
E2EEは信頼できるクライアントがこれらを正しく実装した場合に成立します。
Webページ内のJavaScriptはフラグメントを読めるため、サーバー配信コードの改ざんや
クライアントの侵害に対する保護にはなりません。

UUIDは取得・消費の権限を持つBearer IDです。IDを知る者は復号キーなしでも消費できます。
POSTは送信者を限定するBearer認証を使います。32バイトの暗号学的な乱数を
64文字の小文字hexにした送信認証トークンを、Worker secretの`PUBLISH_TOKEN`へ保存します。
初期の少人数試用向けの共有トークンで、利用者別の失効・保存量制限は未実装です。
WAFレート制限と本番アカウントの監視は公開前に設定・確認します。
アプリケーションは本文、キー、ID、D1例外の詳細をログへ出しません。
Cloudflare側のアクセスログや、前段プロキシ・外部監視の記録は別途確認が必要です。

## ローカル開発

Rustは`rust-toolchain.toml`の1.95.0、`wasm32-unknown-unknown`、Node.js 24/npmを使用します。
Worker SDKとビルドツールは`0.8.7`を使用します。
Wranglerは`4.147.0`を使用します。古いWranglerでは指定した互換日が
古いランタイムの日付へフォールバックする場合があります。
D1対応は`worker`クレートの`d1`機能で有効になるため、別のDBドライバーは不要です。

```sh
export PATH="$HOME/.cargo/bin:$PWD/target/tools/bin:$PWD/node_modules/.bin:$PATH"
rustup show
cargo install worker-build --version 0.8.7 --locked --root target/tools
npm ci
wrangler d1 execute veil-vault --local --file schema.sql
# 次の値は公開済みのテスト専用ダミー。実環境では使わない。
wrangler dev --local --test-scheduled --ip 127.0.0.1 \
  --var SERVICE_ENABLED:true \
  --var PUBLISH_TOKEN:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
```

別ターミナルから、実際の秘密情報を含まないダミー入力で確認できます。

```sh
curl -i http://127.0.0.1:8787/api/secrets \
  -H 'Authorization: Bearer aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' \
  -H 'Content-Type: text/plain; charset=utf-8' \
  --data-binary 'demo-ciphertext-only'
# 応答のUUIDを指定。最初は200、次から404。
curl -i http://127.0.0.1:8787/api/secrets/UUID
```

```sh
cargo fmt --check
cargo check --locked --target wasm32-unknown-unknown
cargo clippy --locked --target wasm32-unknown-unknown -- -D warnings
```

HTTP/D1の統合検証は次のコマンドで実行します。
`tests/run_local.py`が新しい保存先と独立した設定を作り、ローカルWorkerを起動・停止します。
認証・設定不足・停止状態と、`tests/local_api.py`の並行取得・期限・異常系を検証します。
テスト用のダミーデータと障害再現トリガーを作りますが、既存DBの再利用・削除はしません。
テスト状態とダミーの診断ログは`.local/verify-ci-*`に残します。

```sh
worker-build --release
python3 -m unittest discover -s tests -p 'test_*.py'
npm run test:local
```

## 公開・費用

Cloudflareの現在のFree Tier内で小規模に運用可能な設計ですが、無料運用は保証しません。
2026-10-05確認時点のD1 Free枠は読み取り500万行/日、書き込み10万行/日、
合計保存量5 GBです。削除とインデックス更新も利用量へ影響します。
Workers側の制限も含め、利用量と最新料金を確認してください。
[D1料金](https://developers.cloudflare.com/d1/platform/pricing/)、
[Workers料金](https://developers.cloudflare.com/workers/platform/pricing/)を参照してください。

`wrangler.toml`はローカル用です。本番は`wrangler.production.toml`に分離し、
`api.veil-s.com`のCustom Domain設定を含めています。両方とも初期状態はAPI停止です。
DB IDは仮のため、本番デプロイの事前検証で拒否されます。
GitHub Actionsはmainの検証後にデプロイする構成ですが、
`PRODUCTION_DEPLOY_ENABLED=true`の明示的な有効化まではデプロイしません。
リモートDB作成・スキーマ適用、トークン登録、DNS/Custom Domain変更、デプロイは未実施です。

公開の順序と停止・復旧は[運用手順](docs/OPERATIONS.md)、
保証の範囲と未対応の脅威は[セキュリティモデル](docs/SECURITY_MODEL.md)を参照してください。

仕様の根拠:
[Workers Rust](https://developers.cloudflare.com/workers/languages/rust/)、
[worker 0.8.7 D1 API](https://docs.rs/worker/0.8.7/worker/d1/index.html)、
[D1のSQL](https://developers.cloudflare.com/d1/sql-api/sql-statements/)、
[D1のPrepared Statements](https://developers.cloudflare.com/d1/worker-api/prepared-statements/)、
[D1の書き込みはPrimaryへ送られる](https://developers.cloudflare.com/d1/best-practices/read-replication/)、
[SQLite RETURNING](https://www.sqlite.org/lang_returning.html)。
