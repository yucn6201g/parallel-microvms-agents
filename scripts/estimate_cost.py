#!/usr/bin/env python3
"""Lambda MicroVMs でエージェントを並列実行した場合の AWS 費用を試算する。

料金単価は 2026-06 のローンチ時点の公開情報 (第三者による集計を含む) を既定値にしている。
リージョンや改定で変わるため、正式な単価は AWS Lambda の料金ページで確認し、
--vcpu-second / --gb-second などで上書きすること。LLM の利用料 (サブスクリプション / API トークン) は含まない。

例:
  python3 scripts/estimate_cost.py                      # 10 台 x 4GB x 8 時間/日 x 22 日
  python3 scripts/estimate_cost.py --agents 5 --memory-gb 8 --hours-per-day 6
  python3 scripts/estimate_cost.py --suspended-ratio 0.4 # 稼働時間の 40% はアイドルでサスペンド
"""

import argparse

# 既定単価 (USD)。ローンチ時に報告された値の幅を low / high として持つ
PRESETS = {
    "low": {"vcpu_second": 0.0000276944, "gb_second": 0.0000036667, "snapshot_rw_gb": 0.00466},
    "high": {"vcpu_second": 0.0000322421, "gb_second": 0.0000042688, "snapshot_rw_gb": 0.02},
}
SNAPSHOT_STORAGE_GB_MONTH = 0.096   # サスペンド中スナップショット / イメージの保存
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
    snapshot_rw_day = cycles_day * snap_gb * 2 * rates["snapshot_rw_gb"]
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
    p.add_argument("--agents", type=int, default=10, help="同時稼働する MicroVM 数 (既定 10)")
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
    p.add_argument("--snapshot-rw-gb", type=float, help="スナップショット読み書き GB 単価を上書き")
    p.add_argument("--usd-jpy", type=float, default=150, help="円換算レート (既定 150)")
    a = p.parse_args()

    presets = dict(PRESETS)
    if a.vcpu_second or a.gb_second or a.snapshot_rw_gb:
        base = dict(PRESETS["high"])
        base.update({k: v for k, v in (("vcpu_second", a.vcpu_second), ("gb_second", a.gb_second),
                                        ("snapshot_rw_gb", a.snapshot_rw_gb)) if v})
        presets = {"custom": base}

    print(f"前提: {a.agents} 台 x {a.memory_gb:g}GB ({a.memory_gb / 2:g} vCPU) x {a.hours_per_day:g} 時間/日 x "
          f"{a.days_per_month:g} 日/月, サスペンド割合 {a.suspended_ratio:.0%}")
    print()
    header = f"{'単価':<8}{'1台/時':>10}{'計算/日':>11}{'スナップ/日':>13}{'変動/月':>11}{'固定/月':>10}{'合計/月':>11}{'円/月':>12}"
    print(header)
    print("-" * len(header))
    for label, rates in presets.items():
        r = compute(a, rates)
        total = r["variable_month"] + r["fixed_month"]
        print(f"{label:<8}{r['vm_hour']:>10.4f}{r['compute_day']:>11.2f}{r['snapshot_day']:>13.2f}"
              f"{r['variable_month']:>11.2f}{r['fixed_month']:>10.2f}{total:>11.2f}{total * a.usd_jpy:>12,.0f}")
    print()
    print("単位は USD (円/月のみ円)。固定/月 = イメージ保存 + S3 ワークスペース保存 + Secrets Manager + CloudWatch Logs")
    print("含まないもの: LLM 利用料、インターネットへのデータ転送、バースト時 (ベースライン超過) の追加課金の可能性")


if __name__ == "__main__":
    main()
