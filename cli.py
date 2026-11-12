import argparse
from datetime import datetime

from garmin_ai import config
from garmin_ai import sync as sync_module


def _no_mfa():
    raise RuntimeError("Garmin asked for an MFA code; sign in again from the Connections page.")


def main():
    parser = argparse.ArgumentParser(description="Garmin data sync CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    sync_parser = subparsers.add_parser("sync", help="Pull data from Garmin Connect into the local database")
    sync_parser.add_argument("--days", type=int, default=7, help="How many days back to sync (default: 7)")
    sync_parser.add_argument("--unattended", action="store_true",
                             help="Never prompt; append the result to data/sync.log (for scheduled runs)")

    args = parser.parse_args()

    if args.command == "sync":
        if not args.unattended:
            start, end = sync_module.sync(days=args.days)
            print(f"Synced Garmin data from {start} to {end}.")
            return
        log_path = config.PROJECT_ROOT / "data" / "sync.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            start, end = sync_module.sync(days=args.days, prompt_mfa=_no_mfa)
            message = f"Synced {start} to {end}"
        except Exception as error:
            message = f"FAILED: {sync_module.friendly_error(error)}"
        with open(log_path, "a", encoding="utf-8") as log:
            log.write(f"{datetime.now().isoformat(timespec='seconds')} {message}\n")


if __name__ == "__main__":
    main()
