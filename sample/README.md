# 作問用の雛形

Linux / WSL、Python 3.11以上、Rime `bce5de3` で動作確認する3問です。
ネットワークやトークンなしで実行できます。全ソースはPythonの標準ライブラリだけを使います。

| ディレクトリ | 判定 | 含まれるもの |
| --- | --- | --- |
| normal | 通常ジャッジ | 和を求める問題、部分点JSON、正解・誤答 |
| special | スペシャルジャッジ | 正の整数の組、異なる2つの正解・誤答、出力検証器 |
| interactive | インタラクティブ | 秘密の整数の二分探索、対話ジャッジ、ローカル接続、正解・誤答 |

各問に `PROBLEM`、問題文・解説、`tests/TESTSET`、generator、validator、
`solution/SOLUTION` と解答ソースがあります。`wrong/` は意図的な誤答で、
`challenge_cases` によりRimeが「誤答を検出できること」も検証します。

## 実行する

リポジトリ直下で、[READMEのソースからの導入手順](../README.md)に従って
本ツールとRimeを同じvenvへインストールしてから実行します。

~~~console
source .venv/bin/activate
cd sample
rime test
# 1問だけ実行
rime test special
~~~

`rime test` が各問の `rime-out/tests/` に入力 `.in` と参照出力 `.diff` を生成します。
ケース本体は配布・Git管理しません。`tests/` 内でgeneratorを直接実行しないでください。
generatorはRime専用の `script_generator()` として登録しており、
サーバー側generatorの登録や `push --generate` はこの雛形では行いません。
生成器のソースをGitで管理し、生成結果だけを `push --testcases` で同期します。

## 自分の問題へ転用する

1. `sample/` 一式を新しい作業場所へコピーします。生成済みの `rime-out/` はコピー不要です。
   既存Rimeプロジェクトへ問題だけをコピーする場合は、PROJECTの `rime_plus` 導入と
   本ツールの管理ブロックも確認してください。
2. 各 `PROBLEM` の `problem_id=1/2/3` を、自分が編集できる実際のProblemIdに変更します。
   これらは架空のローカル識別用IDであり、サイト上の問題を指す用途ではありません。
   `rime_id`、タイトル、制約、判定方式とソースも編集します。
3. `.env.example` を `.env` へコピーし、実際の問題IDに対応するトークンを設定します。
   `yukitools-rime languages` で言語IDを確認し、各DSLの `lang_id` を合わせます。
4. `rime test` が成功してから、`yukitools-rime push normal --testcases --dry-run` で
   送信予定を確認し、必要な変更だけになっていれば `--dry-run` を外して送信します。

初期設定の `sync=False` はPROJECT全体を対象にした同期から除外するだけです。
**問題ディレクトリを明示したpush/pull等には効きません。IDの変更前に実行しないでください。**
全問をまとめて同期したいときだけ、転用した問題の `sync=True` へ変更します。

`normal/subtask.json` は30点・70点の部分点設定例です。不要ならファイルを外します。
すでにremoteへ登録した部分点を解除する場合は、削除ではなく `{"subtasks": []}` をpushします。

## ジャッジの実装上の違い

`special/tests/judge.py` は、Rimeでは `--infile/--difffile/--outfile`、
yukicoderでは入力パスを `argv[1]`、提出出力を標準入力から受け取ります。
参照出力と文字列比較せず、値の範囲・個数・和を検証します。

`interactive/tests/judge.py` は同じ対話ロジックを3つの入口から使います。

- yukicoder: `argv[1]` から秘密の入力を読み、標準入出力で解答とライブ通信します。
- ローカル: TESTSETの `LocalReactiveRunner` が `--local` と解答コマンドを渡します。
  ジャッジが解答プロセスを起動して入出力を接続し、結果だけを `AC/WA` で記録します。
- Rimeの出力検証: `--outfile` に記録された結果を検証します。

改行・flush、質問回数、範囲外、EOF、終了後の余分な出力を検証します。
ローカル接続には2秒のwatchdogがあります。問題の制限を変える場合は
`PROBLEM` と `judge.py` の両方を調整してください。
このローカル接続はサンドボックスではありません。信頼できる自分のソースにのみ使ってください。
本番公開前にはサーバー上でのテスター検証も必要です。

サーバーの呼出し仕様は
[yukicoderのスペシャル・リアクティブジャッジ仕様](https://yukicoder.me/problems/no/5000)、
flush等の注意点は[リアクティブ問題の案内](https://yukicoder.me/wiki/reactive)を参照してください。

雛形はGit checkoutとsdistに含まれます。wheelによるインストール先には配置されません。
