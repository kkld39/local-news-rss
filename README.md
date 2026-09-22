# 地域別ローカルニュースRSS

任意の地域を `locations.yml` に追加して、地域ニュースRSSを生成できる汎用ツールです。Google Newsの日本向け検索RSSから記事を取得し、地域ごとのRSS 2.0をGitHub Pagesで公開します。GitHub Actionsが原則毎時17分に実行するため、自宅PC・サーバーの常時稼働は不要です。Python 3.12で動作します。

検索は `hl=ja&gl=JP&ceid=JP:ja` を指定します。検索語に合う記事が対象で、地域に関係しない同名の話題が混じる可能性があります。

## 最初にGitHubで行う操作

1. GitHubで公開リポジトリを作成します。既定ブランチは `main` を推奨します。
2. このフォルダの内容をリポジトリのルートにアップロード・commitします。**`.github/workflows/news.yml` と `newsrss`、`tests` も含めてください。** `.venv`、`__pycache__` は不要です。生成済みの `feeds`、`data`、`index.html` は含めて構いません。
3. **Settings → Actions → General** でActionsを有効にし、GitHub公式Actionsを許可します。ページ下部の **Workflow permissions → Read and write permissions** を選び保存します。組織のポリシーやブランチ保護がbotのpushを禁止している場合、リポジトリ管理者による設定が必要です。
4. **Settings → Pages → Build and deployment → Source** を **GitHub Actions** にします。`Deploy from a branch` は選びません。このworkflowが公開用ファイルを直接アップロード・デプロイします。
5. **Actions → Update local news RSS → Run workflow** を選び、既定ブランチで実行します。初回は新規記事の画像取得のため数分かかります。
6. buildとdeployが緑色になったら、Pages設定画面のサイトURLを開きます。`https://<username>.github.io/<repository>/` に地域別リンクが表示されます。
7. Inoreaderの「フィードを追加」等の購読画面に、各リンクのURLを貼り付けます。

現在配信している地域と購読URL：

| 地域 | 検索条件 | RSSパス |
| --- | --- | --- |
| 稚内 | 稚内市 OR 宗谷 | `feeds/wakkanai.xml` |
| 北見 | 北見市 | `feeds/kitami.xml` |
| 多摩 | 多摩市 | `feeds/tama.xml` |

```text
https://<username>.github.io/<repository>/feeds/wakkanai.xml
https://<username>.github.io/<repository>/feeds/kitami.xml
https://<username>.github.io/<repository>/feeds/tama.xml
```

サイトURLはActionsのPages設定から自動取得します。独自ドメインをPagesに設定した場合も反映されます。ローカル生成時にURLを省略すると、indexは相対リンクになります。

## 新しい地域を追加する方法

Pythonコードやworkflowの編集は不要です。

1. GitHubのリポジトリ画面で **locations.yml** を開きます。
2. 鉛筆アイコン **Edit this file** をクリックします。
3. ファイルの末尾へ、以下の3行を貼り付けます。**行頭の空白は既存の地域と揃えてください。タブは使いません。**

```yaml
  - name: 旭川
    slug: asahikawa
    query: "旭川市"
```

既存の `locations:` 配下にある地域の項目に続けて追加します。

4. **Commit changes** で保存します。`main` では自動実行されます。それ以外の既定ブランチでは次の定期実行を待つか手動実行してください。
5. 実行完了後、`feeds/asahikawa.xml` が公開されます。トップページからURLをコピーしてInoreaderに追加します。

`name` は表示名、`slug` はURLのファイル名、`query` はGoogle Newsの検索語です。slugは重複しない半角小文字・数字・ハイフンにしてください。slugを変更すると購読URLも変わります。地域を削除すると、その地域の生成RSS・履歴も次回実行時に削除されます。

## 取得・画像・キャッシュ

- 記事のタイトル、URL、配信元、公開日時、descriptionを取得します。外部descriptionはプレーンテキストへ変換し、RSSに安全なHTMLとして埋め込みます。
- Google Newsの元記事URL解決を試し、失敗時はGoogle News URLで記事を残します。非公開APIによる解決はGoogleの仕様変更で動かなくなる可能性があります。
- 代表画像は `og:image`、`twitter:image`、`itemprop=image`、`image_src` の順に探します。画像ファイルは保存せず元サイトのURLだけを使用します。
- RSSには `media:thumbnail`、`media:content medium="image"`、descriptionの`img`を出力します。Inoreaderでは画像を表示する一覧形式を選んでください。直リンク制限・期限付き画像URL・Inoreader側のキャッシュや表示設定によって表示されない場合があります。画像表示そのものは保証できません。
- 画像取得成功後は原則再アクセスしません。失敗時は24時間後、さらに48時間後まで、合計最大3回だけ試します。再試行上限に達しても記事は残ります。
- 1実行あたり新規・再試行のメタデータ取得は通常全地域合計45記事までです。地域別に均等な枠を割り当て、未取得記事は以降の実行で処理します。地域が45を超える場合は各地域最低1記事分に自動調整します。
- リクエストは逐次実行で最低1秒間隔、接続4秒・読み取り7秒のタイムアウト、レスポンス上限2MB、リダイレクト最大5回です。bot対策の回避やJavaScript実行は行いません。1記事の失敗で全体は停止しません。
- URLはHTTP(S)・標準ポートだけを許可し、内部IP・loopback・link-local等を拒否します。DNSの全回答を確認し、検証したIPへ接続先を固定します。TLSのホスト名・証明書検証は有効です。リダイレクト先も毎回検証します。
- 各地域は**過去30日以内かつ最新150件まで**を保持します。URLと「タイトル＋配信元」で重複排除します。媒体が異なる同じ話題は原則残します。guidは元のGoogle News URLに基づき固定します。
- 履歴は `data/<slug>.json`、画像・解決URL・失敗回数は `data/article_cache.json` に保存します。保持対象外のキャッシュを自動削除します。現在のファイルサイズは制限されますが、Gitの過去のcommit履歴は長期的には増えます。
- 変更がある場合だけ生成物をcommitします。実行日時だけを更新して無駄なcommitを作ることはありません。画像キャッシュのみの変更は保存のためcommitされます。
- 全地域のGoogle News取得に失敗した実行はエラーにして公開を止め、前回の公開内容を維持します。一部地域のみ失敗した場合は履歴からRSSを生成します。

## 手動実行・ローカル開発

GitHub上では **Actions → Update local news RSS → Run workflow** を使用します。定期実行は既定ブランチが対象です。手動実行も既定ブランチを選んでください。

Python 3.12以降の環境で：

```sh
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m newsrss.generate --base-url https://<username>.github.io/<repository>
```

`--max-enrich 9` で実取得検証時の画像取得件数を抑えられます。テストはmockを使うためネット接続不要です。

## トラブルシューティング

- **初回のconfigure-pages/deploy失敗・404**：PagesのSourceがGitHub Actionsか確認し、手動で再実行してください。公開直後は反映に少し時間がかかる場合があります。
- **pushが403・保護ルールで拒否**：Actionsの書き込み権限とブランチ保護・組織ポリシーを確認してください。PAT等の秘密情報は通常不要です。同時に人がcommitした場合は競合を上書きせず失敗するため、再実行してください。
- **地域が増えない**：YAMLのインデント、slugの重複、必須項目を確認してください。Actionsのbuildログに設定エラーが出ます。
- **画像がない**：取得順待ち、画像指定なし、403、タイムアウト、URL解決失敗などが考えられます。`data/article_cache.json` の `error` とActionsログを確認してください。画像がなくてもRSSには残ります。
- **改善後に画像を再取得したい**：該当記事のキャッシュエントリーをJSONから削除して手動実行します。全キャッシュの削除は大量の再アクセスにつながるため避けてください。
- **記事が少ない／0件**：検索語と公開日時を確認してください。30日より古い記事は保存しません。Google News自体の検索結果が少ない場合もあります。
- **毎時17分ちょうどに実行されない**：GitHub Actionsのscheduleは遅延や実行見送りがあり得ます。長期間活動のない公開リポジトリでは定期実行が無効になる場合があるため、Actions画面で再有効化してください。
- **Inoreaderの更新が遅い**：Inoreader側の巡回間隔とキャッシュに依存します。ブラウザでRSS URLを開き、公開ファイルが更新されているか先に確認します。

Google News・各ニュースサイト・GitHub・Inoreaderの仕様変更、アクセス制限、サービス制限によって取得や表示ができなくなる可能性があります。RSS検索結果や画像利用については各提供元の利用条件を確認してください。

GitHub公式資料：[Actionsを使うPages公開](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages)、[Pagesの公開元設定](https://docs.github.com/en/pages/getting-started-with-github-pages/configuring-a-publishing-source-for-your-github-pages-site)。
