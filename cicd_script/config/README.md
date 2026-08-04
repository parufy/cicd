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
