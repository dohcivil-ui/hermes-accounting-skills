#!/usr/bin/env python3
"""Standalone G1 inspection; no application imports, migrations, or SQL writes.

Run only on staging with python3 and --actor-from TRANSACTION_UUID.
Prints a consistent, redacted snapshot for the actor identified by that row.
Compare state_sha256 before/after one test message. Equality proves persisted
state equality for the three inspected tables, not absence of transient writes.
Candidates are database rows, not proof that the live router will select them.
No snapshots or real transaction data are written to files by this script.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from datetime import datetime, timezone
from uuid import UUID

DB = Path('/data/lekza-staging/state/transactions.db')
TABLES = ('transaction_state', 'manual_input_selection', 'pending_manual_input')


def inspect(connection, anchor):
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA query_only = ON')
    connection.execute('BEGIN')
    try:
        actor = connection.execute(
            'SELECT tenant_id, platform, chat_id, telegram_user_id '
            'FROM transaction_state WHERE transaction_id = ?', (anchor,)
        ).fetchone()
        if actor is None:
            raise ValueError('ANCHOR_NOT_FOUND')
        identity = tuple(actor[key] for key in ('platform', 'chat_id', 'telegram_user_id'))
        rows = {}
        for table in TABLES:
            rows[table] = [dict(row) for row in connection.execute(
                'SELECT * FROM ' + table +
                ' WHERE platform = ? AND chat_id = ? AND telegram_user_id = ?', identity
            )]
        transactions = rows['transaction_state']
        if any(row['tenant_id'] != actor['tenant_id'] for row in transactions):
            raise ValueError('AMBIGUOUS_ACTOR_TENANT')
        canonical = {
            table: sorted(json.dumps(row, sort_keys=True, ensure_ascii=True)
                          for row in rows[table]) for table in TABLES
        }
        digest = hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()
        candidates = []
        for row in sorted(transactions, key=lambda item: item['transaction_id']):
            if row['current_state'] in ('confirmed', 'cancelled'):
                continue
            candidates.append({key: row[key] for key in (
                'transaction_id', 'current_state', 'entry_mode', 'version',
                'needs_amount', 'needs_reference'
            )})
        selected = [{key: row[key] for key in ('transaction_id', 'expected_version')}
                    for row in rows['manual_input_selection']]
        return {
            'status': 'READ_ONLY_SNAPSHOT_OK',
            'captured_utc': datetime.now(timezone.utc).isoformat(),
            'scope': 'same_actor_three_tables',
            'state_sha256': digest,
            'nonterminal_candidates': candidates,
            'manual_selection': selected,
            'pending_typed_input_modes': sorted(row['input_mode']
                                               for row in rows['pending_manual_input']),
        }
    finally:
        connection.rollback()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--actor-from', required=True, type=UUID)
    args = parser.parse_args()
    try:
        if os.environ.get('LEKZA_RUNTIME_ENV', 'staging') != 'staging':
            raise ValueError('NOT_STAGING')
        if not DB.is_file() or DB.resolve(strict=True) != DB:
            raise ValueError('STAGING_DB_PATH_REJECTED')
        connection = sqlite3.connect(DB.as_uri() + '?mode=ro', uri=True, timeout=5)
        try:
            result = inspect(connection, str(args.actor_from))
        finally:
            connection.close()
    except (OSError, sqlite3.Error, ValueError, KeyError) as error:
        # Do not print exception text, paths, SQL data, or actor identifiers.
        print(json.dumps({'status': 'INSPECTION_FAILED', 'error_type': type(error).__name__}))
        return 1
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
