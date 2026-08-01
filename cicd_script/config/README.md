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
