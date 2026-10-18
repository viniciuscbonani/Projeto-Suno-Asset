"""Local maintenance commands. No inference, expiration or corpus access."""
import argparse
import json
from pathlib import Path

from suno_asset.config import Settings
from suno_asset.services.local_storage import LocalStorage
from suno_asset.services.artifact_lifecycle import reconcile, delete_source, delete_run, inspect_orphans
from suno_asset.services.protocols import ExpectedServiceError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', type=Path, default=Settings().data_root)
    sub = parser.add_subparsers(dest='command', required=True)
    delete = sub.add_parser('delete-source')
    delete.add_argument('source_id')
    delete_execution = sub.add_parser('delete-run')
    delete_execution.add_argument('request_id')
    for command in (delete, delete_execution, sub.add_parser('orphans')):
        group = command.add_mutually_exclusive_group(required=True)
        group.add_argument('--dry-run', action='store_true')
        group.add_argument('--execute', action='store_true')
    sub.add_parser('reconcile')
    args = parser.parse_args(argv)
    try:
        store = LocalStorage(args.data_root)
        if args.command == 'delete-source':
            result = delete_source(store, args.source_id, execute=args.execute)
        elif args.command == 'delete-run':
            result = delete_run(store, args.request_id, execute=args.execute)
        elif args.command == 'orphans':
            result = inspect_orphans(store, execute=args.execute)
        else:
            result = reconcile(store)
        print(json.dumps(result, sort_keys=True))
        return 0
    except ExpectedServiceError as exc:
        print(json.dumps({'error': exc.code}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
