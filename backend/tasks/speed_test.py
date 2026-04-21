import json
import subprocess
import time
from datetime import UTC, datetime


def run_speedtest():
    try:
        result = subprocess.run(
            ['speedtest-cli', '--json'],
            capture_output=True,
            text=True,
            timeout=120,
        )

        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())

        data = json.loads(result.stdout)

        return {
            'recorded_at': datetime.now(UTC),
            'ping_ms': data['ping'],
            # Divide these numbers by 1 million to get Mbps
            'download_mbps': data['download'] / 1_000_000,
            'upload_mbps': data['upload'] / 1_000_000,
            'server_name': data['server']['name'],
            'server_sponsor': data['server']['sponsor'],
            'server_id': data['server']['id'],
            'server_distance_km': data['server']['d'],
            'client_ip': data['client']['ip'],
            'client_isp': data['client']['isp'],
            'raw_timestamp': data['timestamp'],
        }

    except Exception as exc:
        print(f'[ERROR] Speedtest failed: {exc}')
        return None


def main_loop(interval_seconds=900):
    print('[INFO] Starting speedtest monitor...')

    while True:
        start_time = time.time()

        result = run_speedtest()

        if result:
            print(
                f"[{result['recorded_at'].isoformat()}] "
                f"Ping={result['ping_ms']:.2f} ms | "
                f"Down={result['download_mbps']:.2f} Mbps | "
                f"Up={result['upload_mbps']:.2f} Mbps | "
                f"Server={result['server_name']} ({result['server_sponsor']})"
            )
        else:
            print('[WARN] No result recorded')

        elapsed = time.time() - start_time
        sleep_time = max(0, interval_seconds - elapsed)
        time.sleep(sleep_time)


if __name__ == '__main__':
    main_loop(interval_seconds=60)