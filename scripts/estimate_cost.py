#!/usr/bin/env python3
"""Lambda MicroVMs でエージェントを並列実行した場合の AWS 費用を試算する。

料金単価は、東京リージョン (ap-northeast-1) の 2026-11 の実請求 (Cost Explorer の使用種別ごとの単価) を既定値にしている。
他のリージョンや改定後は AWS Lambda の料金ページで確認し、--vcpu-second / --gb-second などで上書きすること。
既定で日本の消費税 10% を含める (--tax-rate 0 で除外)。LLM の利用料 (サブスクリプション / API トークン) は含まない。

例:
  python3 scripts/estimate_cost.py                      # 2 台 x 4GB x 8 時間/日 x 22 日
  python3 scripts/estimate_cost.py --agents 5 --memory-gb 8 --hours-per-day 6
  python3 scripts/estimate_cost.py --suspended-ratio 0.4 # 稼働時間の 40% はアイドルでサスペンド
"""

import argparse

# 既定単価 (USD)。東京リージョンの 2026-11 の実請求から求めた値
PRESETS = {
    "tokyo": {"vcpu_second": 0.0000322421, "gb_second": 0.0000042688,
              "snapshot_read_gb": 0.00186, "snapshot_write_gb": 0.00466},
}
SNAPSHOT_STORAGE_GB_MONTH = 0.0973  # サスペンド中スナップショット / イメージの保存 ($0.0001333/GB 時)
S3_GB_MONTH = 0.025                 # S3 Standard (東京)
SECRET_MONTH = 0.40                 # Secrets Manager 1 シークレットあたり
LOGS_INGEST_GB = 0.76               # CloudWatch Logs 取り込み (東京)
HOURS_PER_MONTH = 730


def compute(a, rates):
    vcpu = a.memory_gb / 2.0  # ベースライン vCPU はメモリ 2GB あたり 1
    vm_hour = vcpu * rates["vcpu_second"] * 3600 + a.memory_gb * rates["gb_second"] * 3600

    running_hours_day = a.hours_per_day * (1 - a.suspended_ratio)
    suspended_hours_day = a.hours_per_day * a.suspended_ratio
    compute_day = a.agents * running_hours_day * vm_hour

    # サスペンド/レジュームごとのスナップショット書き込み・読み出し (メモリ + 書き込まれたディスク)
    snap_gb = a.memory_gb + a.disk_delta_gb
    cycles_day = a.agents * a.suspend_cycles_per_day
    snapshot_rw_day = cycles_day * snap_gb * (rates["snapshot_write_gb"] + rates["snapshot_read_gb"])
    snapshot_store_day = a.agents * suspended_hours_day * snap_gb * SNAPSHOT_STORAGE_GB_MONTH / HOURS_PER_MONTH

    days = a.days_per_month
    snapshots_per_day = 0 if a.snapshot_interval_min <= 0 else a.hours_per_day * 60 / a.snapshot_interval_min
    s3_stored_gb = a.agents * a.workspace_gb * (1 + snapshots_per_day * a.history_days)
    monthly_fixed = (
        a.image_gb * a.image_versions * SNAPSHOT_STORAGE_GB_MONTH
        + s3_stored_gb * S3_GB_MONTH
        + a.secrets * SECRET_MONTH
        + a.logs_gb_month * LOGS_INGEST_GB
    )
    return {
        "vcpu": vcpu,
        "vm_hour": vm_hour,
        "compute_day": compute_day,
        "snapshot_day": snapshot_rw_day + snapshot_store_day,
        "variable_month": (compute_day + snapshot_rw_day + snapshot_store_day) * days,
        "fixed_month": monthly_fixed,
        "s3_stored_gb": s3_stored_gb,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--agents", type=int, default=2, help="同時稼働する MicroVM 数 (既定 2。クォータ 8GB で 4GB の VM を動かせる台数)")
    p.add_argument("--memory-gb", type=float, default=4, help="1 台のベースラインメモリ GB (既定 4 = 2 vCPU)")
    p.add_argument("--hours-per-day", type=float, default=8, help="1 日あたりの起動時間 (既定 8、上限 8/セッション)")
    p.add_argument("--days-per-month", type=float, default=22, help="月の稼働日数 (既定 22)")
    p.add_argument("--suspended-ratio", type=float, default=0.0, help="起動時間のうちサスペンドしている割合 0-1")
    p.add_argument("--suspend-cycles-per-day", type=float, default=0, help="1 台あたり 1 日のサスペンド/レジューム回数")
    p.add_argument("--disk-delta-gb", type=float, default=2, help="スナップショットに含まれる書き込み済みディスク GB")
    p.add_argument("--workspace-gb", type=float, default=0.2, help="ワークスペーススナップショット 1 回の圧縮サイズ GB")
    p.add_argument("--snapshot-interval-min", type=float, default=30, help="ワークスペース定期保存の間隔 (分)")
    p.add_argument("--history-days", type=float, default=7, help="スナップショット履歴の保持日数")
    p.add_argument("--image-gb", type=float, default=4, help="イメージのスナップショットサイズ GB")
    p.add_argument("--image-versions", type=int, default=2, help="保持するイメージバージョン数")
    p.add_argument("--secrets", type=int, default=4, help="Secrets Manager のシークレット数")
    p.add_argument("--logs-gb-month", type=float, default=1, help="CloudWatch Logs 取り込み量 GB/月")
    p.add_argument("--vcpu-second", type=float, help="vCPU 秒単価を上書き")
    p.add_argument("--gb-second", type=float, help="GB 秒単価を上書き")
    p.add_argument("--snapshot-read-gb", type=float, help="スナップショット読み込み GB 単価を上書き")
    p.add_argument("--snapshot-write-gb", type=float, help="スナップショット書き込み GB 単価を上書き")
    p.add_argument("--snapshot-rw-gb", type=float, help="スナップショットの読み込み・書き込みの両方の GB 単価を上書き")
    p.add_argument("--tax-rate", type=float, default=0.10, help="消費税率 (既定 0.10。日本以外の請求先なら 0)")
    p.add_argument("--usd-jpy", type=float, default=150, help="円換算レート (既定 150)")
    a = p.parse_args()

    if not 0 <= a.tax_rate < 1:
        p.error("--tax-rate は 0 以上 1 未満で指定してください (例: 0.10)")
    presets = dict(PRESETS)
    overrides = {k: v for k, v in (("vcpu_second", a.vcpu_second), ("gb_second", a.gb_second),
                                   ("snapshot_read_gb", a.snapshot_rw_gb), ("snapshot_write_gb", a.snapshot_rw_gb),
                                   ("snapshot_read_gb", a.snapshot_read_gb), ("snapshot_write_gb", a.snapshot_write_gb))
                 if v}
    if overrides:
        presets = {"custom": dict(PRESETS["tokyo"], **overrides)}

    print(f"前提: {a.agents} 台 x {a.memory_gb:g}GB ({a.memory_gb / 2:g} vCPU) x {a.hours_per_day:g} 時間/日 x "
          f"{a.days_per_month:g} 日/月, サスペンド割合 {a.suspended_ratio:.0%}")
    print()
    header = (f"{'単価':<8}{'1台/時':>10}{'計算/日':>11}{'スナップ/日':>13}{'変動/月':>11}{'固定/月':>10}"
              f"{'税抜/月':>11}{'税込/月':>11}{'円/月':>12}")
    print(header)
    print("-" * len(header))
    for label, rates in presets.items():
        r = compute(a, rates)
        before_tax = r["variable_month"] + r["fixed_month"]
        total = before_tax * (1 + a.tax_rate)
        print(f"{label:<8}{r['vm_hour']:>10.4f}{r['compute_day']:>11.2f}{r['snapshot_day']:>13.2f}"
              f"{r['variable_month']:>11.2f}{r['fixed_month']:>10.2f}{before_tax:>11.2f}{total:>11.2f}"
              f"{total * a.usd_jpy:>12,.0f}")
    print()
    print("単位は USD (円/月のみ円、税込)。固定/月 = イメージ保存 + S3 ワークスペース保存 + Secrets Manager + CloudWatch Logs")
    print(f"税込/月は消費税 {a.tax_rate:.0%} を含む (--tax-rate で変更)。1台/時〜税抜/月は税抜")
    print("含まないもの: LLM 利用料、インターネットへのデータ転送、バースト時 (ベースライン超過) の追加課金の可能性")


if __name__ == "__main__":
    main()
