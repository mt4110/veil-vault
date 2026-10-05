# 公開準備と運用手順

更新日: 2026-10-05。リモート操作・自動デプロイの有効化は未実施。

初期運用は、送信者を限定したCLI向けの試用とする。匿名投稿サービスや一般向けの
共有画面は対象にしない。OSの保守はCloudflareに委ねるが、アプリの更新、利用量、
乱用、秘密の管理、障害対応は運用者が引き受ける。

## ローカルと本番の境界

| 項目 | ローカル | 本番 |
| --- | --- | --- |
| 構成 | `wrangler.toml` | `wrangler.production.toml` |
| D1 | `--local`の隔離された状態 | 承認して作成したDBのID |
| ホスト | ループバック | `api.veil-s.com`のCustom Domain |
| 初期状態 | `SERVICE_ENABLED=false` | `SERVICE_ENABLED=false` |
| POST認証 | 公開済みダミートークンによる試験 | Worker secretの`PUBLISH_TOKEN` |
| デプロイ | なし | GitHub Actions、`production`環境 |

`SERVICE_ENABLED`が文字列`true`である場合だけAPIが動く。それ以外と未設定は503。
停止中もCronは掃除を続ける。停止はDB破壊・即時全削除ではない。
`PUBLISH_TOKEN`が未設定または不正な形式ならPOSTは503、GETはIDによる取得を続ける。

## 本番を用意する順序

以下は手順書であり、各リモート変更の実行承認を意味しない。

1. Cloudflareでアカウント、料金プラン、`veil-s.com`のZone有効状態、ネームサーバー
   委任、`api.veil-s.com`の既存DNSレコードを確認する。登録状態Activeとは別に確認する。
2. 専用D1を作成し、承認した空DBに`schema.sql`を一度適用する。本番設定の仮IDを
   実際のDB IDに変更する。`CREATE TABLE IF NOT EXISTS`は既存スキーマを更新しない。
   初期スキーマ適用と今後のマイグレーションは、コードのデプロイから分離する。
3. GitHubの`production`環境を作る。対応プランではデプロイ対象をmainに限定し、
   必要な承認ルールを設定する。環境の保護機能はリポジトリ公開範囲とGitHubプランに
   依存するため、環境を作っただけで承認が強制されると考えない。
4. その環境のSecretsへ`CLOUDFLARE_API_TOKEN`と`CLOUDFLARE_ACCOUNT_ID`を登録する。
   トークンは対象アカウント・Zoneに絞る。Worker Scripts Edit、必要なD1アクセス、
   Custom Domain設定に必要な権限を現在のWranglerの要求と照合する。
   Global API Keyは使わない。WAF設定用の権限をデプロイトークンに足さない。
   Cloudflare/GitHubの管理アカウントは多要素認証と回復手段を確保する。
5. 本番設定が閉じた状態で`python3 scripts/check_deploy.py`を実行する。
   対象DB・ホスト・Cronの差分をレビューし、初回デプロイの承認後にのみリポジトリ
   Variable `PRODUCTION_DEPLOY_ENABLED=true`を設定する。この設定は以降のmain更新を
   自動デプロイする権限の有効化でもある。main以外の手動実行はデプロイしない。
6. 初回はAPIが503のままデプロイする。Custom DomainはDNSと証明書の変更を伴う。
   HTTPS接続、未公開のworkers.dev/preview URL、Cron登録、D1バインディングを確認する。
7. Worker secretの`PUBLISH_TOKEN`を安全な入力経路で登録し、WAFと監視を準備する。
   本番を有効化する変更（`SERVICE_ENABLED=true`）をレビューしてからデプロイする。
8. ダミー暗号文だけで認証拒否、送信→取得→再取得404、HEAD非消費、停止503を確認する。
   リモートでの24並行取得、期限境界とCron動作は専用検証データで別途確認する。
   `veil-env`との実際の暗号文・鍵の互換性が未確認なら実シークレットを投入しない。

自動デプロイはClippyだけでなく、Wasmビルド、認証・停止・入力・並行取得・GC・D1障害
のローカル統合検証、Cargo/npm依存の既知脆弱性チェックを通過してから動く。
監査は`Cargo.lock`とnpm Lockfileが対象であり、未知の脆弱性やインストールした
ビルドツール全体の無欠陥を保証しない。本番スキーマを自動変更するコマンドは含めない。
ActionsはSHA固定、`contents: read`、checkoutの認証情報永続化なし。
CIに本番Secretsを渡さず、デプロイJobのWrangler Actionにだけ渡す。
この構成でも信頼したmainのコードは秘密を使える。管理アカウントとmainの保護が必要。

## 秘密の扱い

送信認証トークンは復号キーとは別物。32バイトの暗号学的な乱数を64文字の小文字hexに
した値を使い、パスワード・UUID・テストの`a`64文字を本番用に流用しない。
共有トークンは初期の少人数試用に限定する。利用者別の失効・監査・クォータは未実装。

- `PUBLISH_TOKEN`: Cloudflare Worker secretと送信クライアントの安全な保存先。
  GitHubに置かない。POSTの`Authorization: Bearer …`で送り、URL・本文に置かない。
- `CLOUDFLARE_API_TOKEN`: GitHub `production`環境のSecret。クライアントに配らない。
- 復号キー: クライアントだけで扱う。Cloudflareへ送らない。
- GETのUUID: 取得権限。ログ、チケット、通知、監視URLに残さない。

トークンを生成・登録する際は、画面録画やログへの表示、シェル履歴・コマンド引数への
値の展開を避ける。Worker secretの登録はダッシュボードの秘密入力、または権限600の
一時ファイルからWranglerの標準入力を使う。ローカルの`.dev.vars`も秘密を含むので
コミットしない。登録したトークンを含むファイルの削除は対象を確認して承認後に行う。

## WAFレート制限

2026-10-05に確認した公式仕様では、Freeは1ルール、10秒の計数・緩和期間、IPによる計数。
式はPathとVerified Botに限定され、HostやMethodで絞れない。
そのため「apiホストのPOSTのみ、5回/10秒」はFreeでそのまま設定できない。

初期試用の候補は次のPath式、5回/10秒、Block、緩和10秒とする。
値は実利用前にCLIの正常な操作頻度と共有IPへの影響を確認して調整する。

```text
starts_with(http.request.uri.path, "/api/secrets")
```

FreeではZone内の同じパスを使う別ホストやGETも対象になる。影響範囲を確認できない
場合は適用を止め、対応プラン・別の保護方式を検討する。JSON APIにブラウザー向け
Challengeを返さない。実際の429とCLIの扱いを確認する。WAFの応答はWorkerを通らず、
WorkerのJSON形式や応答ヘッダーと同じとは限らない。

カウンターはデータセンター単位で遅延があり、許容件数・世界全体の投稿数・費用を
厳密には制限しない。分散IP、トークン漏えい、同じIPの複数利用者を考慮する。
Workerの起動やD1利用量がゼロになる保証でもない。Freeの上限到達はサービス停止に
つながり、Paidでは追加料金があり得る。料金プランを自動変更しない。

## 掃除と監視

時刻はUnix秒なので、掃除は実装済みの次のSQLを使う。
文字列を返す`datetime('now', '-1 day')`との比較へ置き換えない。

```sql
DELETE FROM secrets WHERE created_at <= unixepoch() - 86400;
```

`created_at`のインデックスが掃除の走査量を抑える。削除とインデックス更新もD1利用量
に含まれる。GCが遅延してもGET側の期限判定は有効だが、保管期間は掃除の成功まで延びる。

初期はCloudflareの集計メトリクスでWorkerリクエスト数・エラー・CPU、D1の読み書き行数・
保存量、WAFの緩和件数を確認する。通知を設定できる対象とプランを確認し、利用枠逼迫・
エラー増加時の対応担当を決める。監視に実際のGET URLを使うとデータを消費する。
本文・ヘッダー・IDを通知やエラー追跡サービスへ送らない。

Observabilityは無効。Cron失敗は固定文字列をconsoleへ出すが、worker-rsの現行scheduled
ハンドラはエラーを戻さないため、実行が成功表示でも掃除成功の証拠にはならない。
最初はCron実行状況と、集計SQLによる期限切れ件数（本文・IDなし）を確認する。
継続する期限切れの滞留は障害として扱う。自動アラート配線は本番アカウント確認後に行う。
ログを有効にするなら、URLに含まれるBearer IDの保持・アクセス権・外部転送を先に決める。

## 緊急停止・復旧

1. 自動デプロイを無効化し、進行中のデプロイを確認する。設定変更が直後のmain更新で
   上書きされない状態にする。
2. 緊急時はダッシュボードで`SERVICE_ENABLED=false`を反映し、実際のPOST/GETの503を
   確認する。通常時は同じ変更をActionsから反映する。反映中の既存リクエストまで
   巻き戻るわけではない。停止してもWorkerの呼び出し自体は残るため、大量アクセス時は
   WAFでの遮断など前段の操作も対象と影響を確認して行う。
3. 漏えいした秘密を失効・交換する。共有送信トークンの交換は全送信者に影響する。
4. 原因と集計利用量を確認し、修正を検証してから再開する。実行時設定の緊急変更を
   リポジトリにも反映し、次のデプロイとの差異をなくす。

WorkerのロールバックはDBを巻き戻さない。DBを削除前へ復元すると、取得済み暗号文が
復活する可能性がある。通常の復旧に本番D1のTime Travelを使わない。
調査用の復元は隔離して行い、復元DBを公開バインディングへ接続しない。
どうしても復元する場合は停止したまま承認・データ再公開の判断を行う。
新しい空DBへの切替は再公開を避けるが未取得データを失うため、これも明示承認が必要。

## 公式資料

- [WAFのプラン別仕様と計数の制限](https://developers.cloudflare.com/waf/rate-limiting-rules/)
- [CloudflareのGitHub Actions](https://developers.cloudflare.com/workers/ci-cd/external-cicd/github-actions/)
- [APIトークン権限](https://developers.cloudflare.com/fundamentals/api/reference/permissions/)
- [Custom Domains](https://developers.cloudflare.com/workers/configuration/routing/custom-domains/)
- [D1料金とFree上限到達時の動作](https://developers.cloudflare.com/d1/platform/pricing/)
- [Worker集計メトリクス](https://developers.cloudflare.com/workers/observability/metrics-and-analytics/)
- [GitHubのActionsセキュリティ](https://docs.github.com/en/actions/reference/security/secure-use)
- [GitHubの環境保護とプラン](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments)
