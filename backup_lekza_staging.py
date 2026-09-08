"""One-off staging SQLite backup; no Hermes imports, migrations or network calls.

Run as a file with python3 after placing it in an approved staging directory.
Requires LEKZA_RUNTIME_ENV=staging; paths are fixed and cannot be overridden.
Only a printed DB_BACKUP_PASS means the resulting backup was verified.
Documentation: https://docs.python.org/3.13/library/sqlite3.html#sqlite3.Connection.backup
"""

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from contextlib import closing
from datetime import datetime, timezone


SOURCE = Path('/data/lekza-staging/state/transactions.db')
BACKUP_ROOT = Path('/data/lekza-staging/backups/predeploy')


def checked_backup(source, backup_root, *, limit_seconds=45):
    """Internal helper; main() supplies only the fixed staging paths."""
    if not source.is_file() or source.resolve() != source:
        raise RuntimeError('Source is missing or redirected')
    if not backup_root.is_dir() or backup_root.resolve() != backup_root:
        raise RuntimeError('Backup directory is missing or redirected')
    started = datetime.now(timezone.utc).isoformat()
    deadline = time.monotonic() + limit_seconds
    folder = Path(tempfile.mkdtemp(prefix='flow-alignment-', dir=backup_root))
    partial = folder / 'transactions.partial.db'
    final = folder / 'transactions.db'
    with partial.open('xb'):
        pass
    partial.chmod(0o600)

    def progress(status, remaining, total):
        if time.monotonic() >= deadline:
            raise TimeoutError('Backup time limit exceeded')

    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True,
                                 timeout=1, isolation_level=None)) as src:
        src.execute('PRAGMA query_only=ON')
        if src.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                       "AND name='transaction_state'").fetchone() is None:
            raise RuntimeError('Expected transaction table is missing')
        with closing(sqlite3.connect(partial, timeout=1,
                                     isolation_level=None)) as dst:
            src.backup(dst, pages=128, progress=progress, sleep=0.1)
            mode = dst.execute('PRAGMA journal_mode=DELETE').fetchone()[0]
            if mode.lower() != 'delete':
                raise RuntimeError('Backup is not a standalone database')

    with closing(sqlite3.connect(partial.as_uri() + '?mode=ro', uri=True,
                                 timeout=1, isolation_level=None)) as verify:
        verify.set_progress_handler(
            lambda: int(time.monotonic() >= deadline), 1000)
        if verify.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise RuntimeError('Backup integrity check failed')
        rows = verify.execute('SELECT COUNT(*) FROM transaction_state').fetchone()[0]

    with partial.open('rb') as handle:
        digest = hashlib.file_digest(handle, 'sha256').hexdigest()
        os.fsync(handle.fileno())
    # Exclusive publication: never overwrite an existing backup.
    os.link(partial, final)
    partial.unlink()
    result = {
        'status': 'DB_BACKUP_PASS',
        'source': str(source),
        'backup': str(final),
        'started_utc': started,
        'completed_utc': datetime.now(timezone.utc).isoformat(),
        'bytes': final.stat().st_size,
        'sha256': digest,
        'integrity_check': 'ok',
        'transaction_count': rows,
    }
    with (folder / 'manifest.json').open('x', encoding='utf-8') as handle:
        json.dump(result, handle, indent=2)
        handle.write('\n')
        handle.flush()
        os.fsync(handle.fileno())
    return result


def main():
    try:
        if os.environ.get('LEKZA_RUNTIME_ENV') != 'staging':
            raise RuntimeError('Staging environment is required')
        result = checked_backup(SOURCE, BACKUP_ROOT)
    except Exception as exc:
        print(json.dumps({'status': 'DB_BACKUP_FAIL',
                          'error_type': type(exc).__name__}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
