import argparse

from garmin_ai import sync as sync_module


def main():
    parser = argparse.ArgumentParser(description="Garmin data sync CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    sync_parser = subparsers.add_parser("sync", help="Pull data from Garmin Connect into the local database")
    sync_parser.add_argument("--days", type=int, default=7, help="How many days back to sync (default: 7)")

    args = parser.parse_args()

    if args.command == "sync":
        start, end = sync_module.sync(days=args.days)
        print(f"Synced Garmin data from {start} to {end}.")


if __name__ == "__main__":
    main()
