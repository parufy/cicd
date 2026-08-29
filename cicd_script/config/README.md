# シナリオ簡略化設定

シナリオ YAML では `use` に operation 名を指定できます。
細かなパラメータは `action_defaults.yaml` と `operations.yaml` に分離し、シナリオ側では変更したい値だけ `params` に書きます。

## マージ順

後ろにある値ほど優先されます。

```text
action_defaults.yaml の action 共通値
  < operations.yaml の operation 固有値
  < シナリオ YAML の params
```

## シナリオ例

```yaml
config:
  hosts: ../config/hosts.yaml
  defaults: ../config/action_defaults.yaml
  operations: ../config/operations.yaml

scenarios:
  - name: UDP iperf 10秒
    use: iperf.adb_udp_ul_10s

  - name: UDP iperf 30秒 5Mbps
    use: iperf.adb_udp_ul_30s
    params:
      bandwidth: 5M

  - name: wait 3秒
    use: wait.3s
```

既存の `action` / `execution` / `params` を直接書く形式も引き続き利用できます。

## チャンネル別ATT値の一括処理

`mode: set_multi` では、1回のSSH接続・機器初期化内で、チャンネルごとに
異なるATT値を連続設定できます。異なる値の設定はチャンネルごとのAPI呼び出しに
なるため完全な同時設定ではありませんが、複数Actionに分けるより高速です。

```yaml
scenarios:
  - name: CH1-4 ATT設定
    action: vatt_control
    params:
      mode: set_multi
      settings:
        - channel: 1
          attenuation_db: 10.0
        - channel: 2
          attenuation_db: 20.0
        - channel: 3
          attenuation_db: 30.0
        - channel: 4
          attenuation_db: 40.0
```

## 複数チャンネルRampの同時開始

`mode: ramp_multi` では、チャンネルごとに異なるRamp設定を指定できます。
全チャンネルのパラメータを設定した後、VaunixのマルチチャンネルAPIで開始します。

```yaml
scenarios:
  - name: CH1-4 Ramp開始
    action: vatt_control
    execution: sequential
    params:
      mode: ramp_multi
      ramps:
        - channel: 1
          start_db: 0.0
          stop_db: 20.0
          step_db: 1.0
          dwell_ms: 1000
          repeat: true
        - channel: 2
          start_db: 10.0
          stop_db: 50.0
          step_db: 2.0
          dwell_ms: 500
          repeat: true
```

同じ方向・`repeat`・`bidirectional` のチャンネルは1回のAPI呼び出しで開始されます。
これらのモードが異なるチャンネルは、モード別にまとめて開始APIを連続呼び出しします。
同一ATT機器に対する複数のRampステップを `execution: parallel` で実行しないでください。

複数チャンネルのRampを一括停止する場合は、`mode: stop_ramp_multi` と
停止対象の `channels` を指定します。内部では対象チャンネルを順番に選択し、
チャンネルごとの停止APIを連続実行します。指定していないチャンネルは停止しません。

```yaml
scenarios:
  - name: CH1・CH3 Ramp停止
    action: vatt_control
    params:
      mode: stop_ramp_multi
      channels: [1, 3]
```

## repeat の使い方

同じ手順を繰り返す場合は `repeat` と `steps` を指定します。
繰り返し内の step 名には自動で `(1/3)` のような回数が付きます。

```yaml
scenarios:
  - repeat: 3
    steps:
      - name: 機内モード OFF
        use: adb.airplane_off
        params:
          output_file: adb_mode_off_result_{repeat}.txt

      - name: 状態確認
        use: adb.status
        params:
          output_file: adb_status_result_{repeat}.txt

      - name: wait
        use: wait.3s
```

文字列には以下のプレースホルダを使用できます。

- `{repeat}` または `{repeat_index}`: 現在の繰り返し番号。1 始まり。
- `{repeat_count}`: 繰り返し総数。
- `{step}`: repeat ブロック内の step 番号。1 始まり。

## SSH踏み台の切り替え

踏み台が必要なホストでは、ホスト定義の `jumps` に踏み台を手前から順に指定します。
`jumps` が1件なら「踏み台 → 対象ホスト」の2段SSH、2件なら3段SSHです。
`jumps` を省略するか空リスト (`jumps: []`) にすると、GitLab Runnerから対象ホストへ
従来どおり直接SSH接続します。この設定はping、iperf、ADB、VATT、tcpdumpに共通です。

```yaml
hosts:
  - name: target_server
    address: 192.0.2.30
    user: target-user
    password: "${TARGET_SERVER_PASSWORD}"
    jumps:
      - address: 192.0.2.10
        user: jump1-user
        password: "${JUMP1_PASSWORD}"
      - address: 192.0.2.20
        user: jump2-user
        password: "${JUMP2_PASSWORD}"
```

踏み台を使わない場合は、同じ対象ホストを次のように定義します。

```yaml
hosts:
  - name: target_server
    address: 192.0.2.30
    user: target-user
    password: "${TARGET_SERVER_PASSWORD}"
    port: 22
```

踏み台を経由した接続では、GitLab Runner側から最初の踏み台へ、各踏み台から次の
踏み台または対象ホストへTCP/22で到達できる必要があります。

## tcpdump取得

開始と停止では同じ `capture_id` を指定してください。開始はリモート上でバックグラウンド
実行され、停止時にSIGINTでpcapを確定して、デフォルトでは成果物ディレクトリへ回収します。

```yaml
scenarios:
  - name: tcpdump開始
    action: tcpdump
    params:
      host: tcpdump_pc
      mode: start
      capture_id: ue_test_01
      interface: eth0
      filter: "host 198.51.100.25 and port 443"

  # この間に試験ステップを実行

  - name: tcpdump停止・回収
    action: tcpdump
    params:
      host: tcpdump_pc
      mode: stop
      capture_id: ue_test_01
      output_file: ue_test_01.pcap
```

通常はリモートユーザーがパスワードなしで `sudo -n tcpdump`、`kill`、`chmod` を
実行できる必要があります。rootユーザーまたはcapture capabilityを付与したtcpdumpを
使う場合は `sudo: false` を指定できます。主なパラメータは次のとおりです。

- `remote_file`: リモートpcapパス（既定 `/tmp/tcpdump_<capture_id>.pcap`）
- `pid_file`: PIDファイル（既定 `/tmp/tcpdump_<capture_id>.pid`）
- `download`: 停止時にpcapを回収するか（既定 `true`）
- `packet_count`: 指定パケット数で自動終了（`0` は停止操作まで継続）。自動終了後も
  停止アクションを実行するとpcapを回収できます。
- `snaplen`: tcpdumpのsnaplen（`0` はtcpdump側の既定動作）
- `tcpdump_path`: リモートtcpdumpコマンドのパス
