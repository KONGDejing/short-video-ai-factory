#!/usr/bin/env python3
"""Install isolated creator-portal browser services; sessions remain private."""
import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from services.creator_connector import PLATFORMS, install_service
from services.factory_platform_policy import automation_enabled


def install_kuaishou_check():
    units = Path.home() / '.config/systemd/user'
    name = 'short-video-ai-factory-kuaishou-transport'
    (units / f'{name}.service').write_text(f'''[Unit]
Description=Content Factory / Kuaishou transport check
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory={ROOT / 'backend'}
ExecStart={json.dumps(sys.executable)} -m services.factory_transport_health
TimeoutStartSec=45
UMask=0077
''', encoding='utf-8')
    (units / f'{name}.timer').write_text('''[Unit]
Description=Check Kuaishou transport every five minutes

[Timer]
OnBootSec=30s
OnUnitInactiveSec=5min
AccuracySec=10s

[Install]
WantedBy=timers.target
''', encoding='utf-8')
    subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True, capture_output=True, timeout=20)
    subprocess.run(['systemctl', '--user', 'enable', '--now', f'{name}.timer'],
                   check=True, capture_output=True, timeout=30)
    subprocess.run(['systemctl', '--user', 'start', f'{name}.service'], check=True, capture_output=True, timeout=45)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", nargs="+", choices=list(PLATFORMS), default=[p for p in PLATFORMS if automation_enabled(p)])
    args = parser.parse_args()
    for platform in args.platform:
        if platform == 'kuaishou':
            subprocess.run([sys.executable, str(ROOT / 'scripts/beijing_proxy.py'), '--install', '--platform', platform],
                           check=True, timeout=35)
        result = install_service(platform, start=platform != 'kuaishou')
        if platform == 'kuaishou':
            install_kuaishou_check()
        print(f"Installed independent service: {result['service']}")
