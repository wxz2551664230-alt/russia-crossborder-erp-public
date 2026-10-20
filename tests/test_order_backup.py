"""Exercise the actual backup program embedded in the deployment script."""
import sqlite3
import subprocess
import sys
from pathlib import Path


def test_online_order_backup_includes_wal_and_reopens(tmp_path):
    source = tmp_path / 'source.db'
    database = sqlite3.connect(source)
    try:
        database.execute('PRAGMA journal_mode=WAL')
        database.execute('CREATE TABLE ozon_orders (id TEXT, recipient_enc TEXT)')
        database.execute('CREATE TABLE batch_order_groups (id TEXT)')
        database.execute("INSERT INTO ozon_orders VALUES ('order', 'encrypted-recipient')")
        database.execute("INSERT INTO batch_order_groups VALUES ('group')")
        database.commit()
        assert Path(str(source) + '-wal').stat().st_size > 0
        shell = (Path(__file__).resolve().parents[1] / 'deploy/backup-pilot.sh').read_text()
        program = shell.split('<<\'PY\'\n', 1)[1].split('\nPY\n', 1)[0]
        program = program.replace('file:/var/lib/erp/orders/pod/orders.db?mode=ro',
                                  source.as_uri() + '?mode=ro')
        result = subprocess.run([sys.executable, '-c', program], capture_output=True, timeout=30)
        assert result.returncode == 0, result.stderr.decode()
        restored = tmp_path / 'restored.db'
        restored.write_bytes(result.stdout)
        check = sqlite3.connect(restored)
        try:
            assert check.execute('PRAGMA integrity_check').fetchone() == ('ok',)
            assert check.execute('SELECT * FROM ozon_orders').fetchall() == [('order', 'encrypted-recipient')]
            assert check.execute('SELECT count(*) FROM batch_order_groups').fetchone() == (1,)
        finally:
            check.close()
    finally:
        database.close()
