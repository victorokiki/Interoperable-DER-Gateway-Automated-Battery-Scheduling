"""
capstone/operator_control.py
Local operator safety commands for the EMS Bridge.

    python -m capstone.operator_control force-idle --by alice --reason "site work"
    python -m capstone.operator_control release    --by alice
    python -m capstone.operator_control status

Force Idle has precedence over every schedule (see scheduler.py): while it
is engaged the gateway holds the battery at 0 kW. It is stored in the
gateway's schedule database, survives gateway restarts, and stays engaged
until an operator releases it; the running gateway applies a change within
a second and records it in the audit log. Run this from the gateway's
working directory (schedule_store/ is a relative path).
"""

import argparse
import sys
from datetime import datetime, timezone

from capstone.scheduler import get_force_idle, set_force_idle


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m capstone.operator_control",
                                     description="Operator Force Idle override for the EMS Bridge.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, text in (("force-idle", "hold the battery at 0 kW, ahead of every schedule"),
                       ("release", "hand dispatch back to the approved schedule")):
        cmd = sub.add_parser(name, help=text)
        cmd.add_argument("--by", required=True, help="operator name (recorded in the audit log)")
        cmd.add_argument("--reason", default="", help="why (recorded in the audit log)")
    sub.add_parser("status", help="show the current override")
    args = parser.parse_args(argv)

    if args.command in ("force-idle", "release"):
        set_force_idle(args.command == "force-idle", args.by, args.reason)

    current = get_force_idle()
    when = (datetime.fromtimestamp(current["set_at"], tz=timezone.utc).isoformat(timespec="seconds")
            if current["set_at"] else "never set")
    print(f"Force Idle: {'ENGAGED' if current['active'] else 'released'} "
          f"(by {current['by'] or '-'}, {when}) {current['reason'] or ''}".rstrip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
