# yukitools-rime

Rimeのプロジェクト構成をそのまま使い、yukicoderの問題設定・問題文・生成器・
validator・ジャッジコード・解説・テストケース・提出を管理するPython製CLIです。

実装の基準として、Rime (`bce5de3`) と yukicoder_tools (`128249f`) を参照しています。
本プロジェクトはApache License 2.0で提供します。

## 要件とインストール

Python 3.11 以上が必要です。CLI 単体では Rime は必須依存ではなく、Rime の
PROJECT を実行するときだけ遅延 import します。

### GitHub Release からインストールする (推奨)

[Releases](https://github.com/harurunrunrun/yukitools-rime/releases) では、OS に依存しない
Python wheel とソースアーカイブを公開します。Linux / WSL では次の手順で専用の
virtual environment に wheel を直接インストールできます。

~~~console
python3 --version  # 3.11 以上であることを確認
python3 -m venv ~/.venvs/yukitools-rime
. ~/.venvs/yukitools-rime/bin/activate
python -m pip install --upgrade pip
VERSION=0.1.2
python -m pip install "https://github.com/harurunrunrun/yukitools-rime/releases/download/v${VERSION}/yukitools_rime-${VERSION}-py3-none-any.whl"
yukitools-rime --version
yukitools-rime --help
~~~

新しいシェルを開いたときは `. ~/.venvs/yukitools-rime/bin/activate` を再実行して
ください。PyPI では公開していないため、`pip install yukitools-rime` では導入
できません。システムのPythonへ `sudo pip` でインストールせず、上記のvirtual
environmentを使用してください。

### git clone してソースからビルドする

Git と Python 3.11 以上を用意し、次をそのまま実行します。

~~~console
git clone https://github.com/harurunrunrun/yukitools-rime.git
cd yukitools-rime
python3 --version  # 3.11 以上であることを確認
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip build
python -m build
python -m pip install --force-reinstall dist/yukitools_rime-0.1.2-py3-none-any.whl
yukitools-rime --version
yukitools-rime --help
~~~

`python -m build` は `dist/` に wheel と sdist を作ります。コマンドを使うシェルでは
毎回、このclone内の `. .venv/bin/activate` を先に実行してください。

### Rime も同じ環境へインストールする

この手順にはGitも必要です。Rime の PROJECT が `yukitools_rime.rime_plugin` を
import できるように、上で本ツールを
導入したものと同じvirtual environmentを有効にしてから、検証済みのRimeを入れます。

~~~console
python -m pip install "git+https://github.com/icpc-jag/rime.git@bce5de31031e81e64ec9b839ec949724678c1a8d"
rime help
~~~

既にRimeをcloneしている場合は、同じ環境で `python -m pip install -e /path/to/rime`
としても構いません。CLIだけを使い、Rimeの設定を実行しない場合、この導入は不要です。

### 更新する

Release版は新しいRelease番号を `VERSION` へ指定して更新します。

~~~console
. ~/.venvs/yukitools-rime/bin/activate
VERSION=0.1.2
python -m pip install --upgrade "https://github.com/harurunrunrun/yukitools-rime/releases/download/v${VERSION}/yukitools_rime-${VERSION}-py3-none-any.whl"
yukitools-rime --version
~~~

clone版はcloneしたディレクトリへ移動し、次のように更新します。buildが表示した
新しいwheel名を指定してください。同じバージョンのmain上の変更も確実に反映するため、
`--force-reinstall`を使います。

~~~console
. .venv/bin/activate
git pull --ff-only
python -m build
python -m pip install --upgrade --force-reinstall dist/yukitools_rime-0.1.2-py3-none-any.whl
yukitools-rime --version
~~~

### 開発用インストール

clone内にvirtual environmentを作って有効にしたあと、editable modeで開発用依存も
導入します。ソースの変更は再インストールなしで反映されます。

~~~console
python -m pip install -e ".[dev]"
~~~

## クイックスタート

~~~console
yukitools-rime init ./contest
cd ./contest
cp .env.example .env
# .env に認証情報を設定
yukitools-rime new 12345 --dir a
~~~

init は PROJECT の管理ブロック、.env.example、.gitignore を追加します。
ネットワークアクセスと git init は行いません。既存ファイルの管理ブロック外は
保持され、再実行しても同じ結果になります。

代表的な構成は次のとおりです。

~~~text
contest/
  PROJECT
  .env                         # Git 対象外
  a/
    PROBLEM
    statement.md | statement.html
    editorial.md | editorial.html
    tests/
      TESTSET
      generator.cpp
      validator.cpp
      judge.cpp
    solution/
      SOLUTION
      main.cpp
    rime-out/
      tests/
        sample01.in            # Git 対象外
        sample01.diff          # Git 対象外
~~~

設定、文書、generator、validator、judge、solution のソースは Git で管理します。
rime-out/ 以下の生成済みテストケースは一切 Git で管理しません。init が
.gitignore の管理ブロックへ .env と rime-out/ を追加します。

## コマンド一覧

| コマンド | 用途 |
| --- | --- |
| yukitools-rime init [PROJECT] | Rime プロジェクトへ設定を導入 |
| yukitools-rime new <問題ID> [--project PATH] [--dir NAME] [--testcases] | 問題を新規取得 |
| yukitools-rime pull [TARGET] [--testcases] [--yes] | remote を local へ反映 |
| yukitools-rime diff [TARGET] [--testcases] [--exit-code] | remote→local の差分表示 |
| yukitools-rime push [TARGET] [--testcases] [--dry-run] [--prune] [--generate] [--no-wait-compile] | local を remote へ反映 |
| yukitools-rime submit [SOLUTION] [--no-wait] | Rime の解答を提出して判定を待つ |
| yukitools-rime solution <提出ID> [TARGET] (--summary TEXT または --delete) | 想定解を登録・解除 |
| yukitools-rime testcases [TARGET] [--which in または out] | remote のケース名を表示 |
| yukitools-rime languages [--include-disabled] | 言語 ID を表示 |

new は PROJECT 直下へ問題を作り、問題設定、問題文、登録済み generator、validator、judge を
取得します。既定ディレクトリ名は問題 ID です。既存ディレクトリ、重複問題 ID、
直下でない --dir は拒否します。ケース本文は --testcases 指定時だけ取得します。

pull は問題単位の全リソースを取得・検証してから書き込みます。rime_id、
reference_solution、sync、rime_options、既存の src、prefix、rime_kind は保持します。
remote に generator、validator、judge がない場合はローカル宣言とソースを削除せず
警告します。問題文形式が Markdown と HTML の間で変わった場合だけ旧拡張子を
削除し、ローカルに解説がなければ remote のテンプレートを新規保存しません。

diff は読み取り専用です。設定はフィールド単位、文書は unified diff、
generator/validator/judge は設定とソース、ケースは名前と生バイトで比較します。
通常は差分があっても終了 0、--exit-code 指定時は差分があれば終了 3 です。

push は全対象を事前検証し、変更がある問題本体、generator、judge、解説、
テストケース、validator だけを送ります。validator はテストケースの更新・削除と
サーバー正規化後に処理し、変更がないソースは再送しません。validator が登録済みなら、
ケースだけを変更した場合も再検証結果を待ちます。AC 以外はコンパイルメッセージまたは
失敗ケースを表示して終了 1、時間切れは警告になります。

--dry-run は計画だけを表示します。
--generate はサーバー側で generator を実行する要求であり、ローカルの Rime は
起動しません。--no-wait-compile は judge と validator の結果待ちを省略します。
CLI は rime test を自動実行しないため、ローカル生成ケースを送る前に利用者または
CI が Rime を実行してください。remote 更新は非トランザクションであり、途中失敗
時には完了済みリソースを確認してから再実行してください。

submit は所属問題、ソース、yukicoder の lang_id を yukicoder_solution() から
取得し、既定では最大 10 分、5 秒間隔でジャッジ結果を待って status と実行時間を表示します。
--no-wait を指定すると提出 ID の表示後に待たず終了します。solution は既存提出を
想定解として登録または解除します。testcases は
本文を取らず remote の名前だけを表示します。languages は匿名 API を使うため、
プロジェクト外でも実行できます。

## TARGET と終了コード

TARGET 省略時は現在位置です。PROJECT なら管理対象の全問題、問題、直下の TESTSET、
直下の SOLUTION またはその配下なら所属する 1 問を対象にします。問題は PROJECT
直下だけ、TESTSET と SOLUTION は問題直下だけを探索します。resolve 後に
プロジェクト内であることを検証し、symlink による脱出も拒否します。

`yukicoder_problem(sync=False)` の問題は、PROJECT を対象にした pull、diff、push
から除外されます。トークン失効中など、1 問だけプロジェクト全体の同期を止める用途です。
その問題または配下を明示的に TARGET にすれば同期でき、sync は API へ送られず pull でも
保持されます。

| コード | 意味 |
| ---: | --- |
| 0 | 成功 |
| 1 | API、認証、ファイル操作などの実行エラー |
| 2 | 引数・利用方法のエラー |
| 3 | diff --exit-code で差分あり |
| 130 | 割り込み |

## 認証とセキュリティ

PROJECT 直下の .env またはプロセス環境変数を次の順で解決します。同じキーでは
環境変数が .env より優先されます。

1. YUKICODER_TOKEN_<問題ID>
2. YUKICODER_TOKEN
3. YUKICODER_API_KEY

~~~dotenv
YUKICODER_TOKEN_12345=ypt_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
YUKICODER_TOKEN=ypt_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
~~~

.env は厳密な KEY=VALUE 形式です。値全体の単一・二重引用符は使えますが、
変数展開、エスケープ、行末コメント、複数行値は解釈しません。認証値を
コマンドライン引数に渡す機能はなく、ログ、例外、dry-run に値を出しません。
.env は必ず Git 対象外のままにしてください。

## Rime 設定 DSL

init は既存 use_plugin() より後ろの管理ブロックへ次を置きます。install() は
先に導入されたプラグインの target class に adapter を重ね、Rime の再読込時に
多重登録しません。

~~~python
# BEGIN YUKITOOLS-RIME
from yukitools_rime.rime_plugin import install, yukicoder_project

install()
yukicoder_project(
    base_url="https://yukicoder.me/api",
    rime_out_dir="rime-out",
)
# END YUKITOOLS-RIME
~~~

PROBLEM の唯一の問題定義は次の形です。

~~~python
yukicoder_problem(
    problem_id=12345,
    title="Example",
    tags="",
    level=2.0,
    time_limit_ms=2000,
    memory_limit=512,
    eps_mode="-",
    eps="0",
    wip=True,
    recruiting_tester=False,
    problem_type=0,
    judge_type=0,
    show_ans=False,
    enable_pure_judge=False,
    force_single_server_judge=False,
    allowed_langs=[],
    rime_id="A",
    reference_solution=None,
    rime_options={},
    sync=True,
)
~~~

TESTSET には generator、validator、judge を最大 1 個ずつ置きます。

~~~python
yukicoder_generator(
    lang_id="cpp20",
    src="generator.cpp",
    test_case_num=10,
    prefix=None,
    rime_kind="cxx",
    rime_options={},
)
yukicoder_judge(
    lang_id="cpp20",
    src="judge.cpp",
    rime_kind="cxx",
    rime_options={},
)
yukicoder_validator(
    lang_id="cpp20",
    src="validator.cpp",
    rime_kind="cxx",
    rime_options={},
)
~~~

validator の公開シグネチャは次のとおりです。

~~~python
def yukicoder_validator(
    *,
    lang_id: str,
    src: str,
    rime_kind: str | None = None,
    rime_options: dict[str, object] | None = None,
) -> None: ...
~~~

既知の rime_kind は対応する Rime validator へ委譲し、None なら同期専用として
Rime の生成処理から除外します。

SOLUTION は次の形です。

~~~python
yukicoder_solution(
    lang_id="cpp20",
    src="main.cpp",
    rime_kind="cxx",
    challenge_cases=[],
    rime_options={},
)
~~~

lang_id は yukicoder の ID、rime_kind は c、cxx、java、kotlin、rust、go、script
など Rime 側の実装名で、両者は独立しています。未知言語は rime_kind=None として
generator、validator、judge、solution の同期だけできます。rime_options は
Rime/Rime Plus の directive へ渡されます。

CLI は設定を実行せず AST で読みます。専用呼び出しにはリテラル引数だけを使い、
動的式、未知引数、重複呼び出しは設定エラーになります。ツールは BEGIN/END 内だけ
を編集し、外側の内容と改行形式を保持します。ソースは宣言と同じディレクトリの
通常ファイルに限定し、絶対パス、..、深いサブディレクトリを拒否します。

## テストケースの同期

同期先は <problem>/<rime_out_dir>/<testset-name>/ の直下です。remote の foo.txt は
local の foo.txt.in と foo.txt.diff に対応します。stem は完全に一致し、両方が
非空の通常ファイルでなければなりません。名前に使える文字はサーバーの
`GET /v1/testcase_name_rule` を取得して検証します。現在の規則では ASCII 英数字、
ピリオド、アンダースコア、ハイフンを使用できます。サーバーが許可していても、
空名、.、..、先頭ピリオド、パス区切り、Windows予約名、末尾のピリオドや空白など、
ポータブルなパスとして危険な名前は拒否します。

diff と push は `GET /file/{in|out}?detail=1` の SHA-256 をローカルの生バイトと
比較します。内容が同じケースはダウンロードせず、変更のあるケースだけを取得するため、
大きなテストセットでも不要な通信を抑えます。

pull --testcases で既存 rime-out と remote が違う場合、問題ごとに追加・変更・削除
件数を警告し、remote の完全なスナップショットで置換するか一度確認します。承認時
はローカルだけのケースも削除し、拒否時はケースを一切変更せず他の設定の pull を
続けます。--yes は全確認を承認します。非 TTY で差分があり --yes がなければケース
を変更せず終了 1 です。出力先がまだ存在しない場合だけ確認なしで初期配置します。

push --testcases でケース 0 件またはペア不完全なら停止します。--prune は
--testcases とだけ併用でき、remote にしかないケースを削除します。これにより空の
rime-out による全削除を防ぎます。送信後にサーバーが正規化した内容は再取得して
rime-out へ反映しますが、同ディレクトリは Git 対象外です。

問題文・解説は問題直下の .md または .html の一方だけを許可し、UTF-8、BOM なし、
LF に正規化します。空本文の push は拒否します。ケースだけは生バイトのまま扱います。

## 既存プロジェクトからの移行

yukicoder_tools または yuki-tool とのコマンド・設定互換性はありません。
yukicoder.toml と problem.toml も読みません。通常の Rime 問題の自動変換も
行いません。

移行前に commit またはバックアップを取り、init 後に各 problem() を
yukicoder_problem() へ、必要な TESTSET/SOLUTION directive を上記の専用呼び出しへ
手動で置き換えてください。rime_id と reference_solution は既存設定に合わせます。
最初は diff で確認し、ケースが必要なときだけ --testcases を明示してください。

## 開発、ライセンス、謝辞

テストデータはリポジトリへ fixture として置かず、pytest の tmp_path 内で生成します。
実 API 書き込みや実トークンは自動テストで使いません。

~~~console
ruff check .
ruff format --check .
mypy
python -m pytest -W error --import-mode=importlib --cov=yukitools_rime \
  --cov-report=term-missing --cov-report=json:coverage.json
python tools/check_coverage.py coverage.json
python -m build
python tools/verify_distribution.py --dist-dir dist --static-only
~~~

CI は Linux の Python 3.11〜3.14 と Windows の Python 3.11 / 3.14 でテストし、
warnings-as-errors、importlib mode、未丸めの statement / branch coverage を検証します。
全体はそれぞれ95%以上、主要モジュールは個別にそれぞれ90%以上が必須です。wheelと
sdistのfresh install、console entrypoint、Rime bce5de3の実設定load/buildも確認します。

ライセンスは [Apache License 2.0](LICENSE) です。設計と API 調査では
[yukicoder_tools 128249f](https://github.com/yuki2006/yukicoder_tools/commit/128249f179d76fbb024196d0e74abda8feb4d8a6)
を、Rime 統合では
[Rime bce5de3](https://github.com/icpc-jag/rime/commit/bce5de31031e81e64ec9b839ec949724678c1a8d)
を参照しました。両プロジェクトの作者・貢献者に感謝します。
