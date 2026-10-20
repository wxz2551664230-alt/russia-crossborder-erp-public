"""Atomic shared-order updates using the original manager's business methods."""
from contextlib import closing
from copy import copy
import hashlib
import json
import sqlite3
import threading

from .legacy_engine import OrderError


class _BorrowedConnection:
    """Nested manager methods cannot commit or close the outer transaction."""
    def __init__(self, connection):
        self.connection = connection

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def commit(self):
        pass

    def close(self):
        pass

    def rollback(self):
        raise RuntimeError('Nested order operation requested rollback')


def apply_once(orders, receipt_key, payload, apply, *, private_result=False):
    """Commit the callback's SQLite writes and summary receipt together.

    The callback must only apply already-fetched facts, never call a supplier.
    It gets a private manager clone; the shared manager is never monkey-patched.
    SQLite serializes other writers and the clone supports sequential to_thread
    calls used by the original asynchronous purchase reconciliation.
    """
    if not isinstance(receipt_key, str) or not 1 <= len(receipt_key) <= 160:
        raise OrderError('回执编号无效', status_code=409)
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                      separators=(',', ':')).encode()).hexdigest()
    with closing(sqlite3.connect(orders.path, timeout=30, check_same_thread=False)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        connection.execute('BEGIN IMMEDIATE')
        try:
            connection.execute('CREATE TABLE IF NOT EXISTS erp_operation_receipts '
                               '(receipt_key TEXT PRIMARY KEY, digest TEXT NOT NULL, summary_json TEXT NOT NULL)')
            previous = connection.execute('SELECT digest,summary_json FROM erp_operation_receipts '
                                          'WHERE receipt_key=?', (receipt_key,)).fetchone()
            if previous:
                if previous['digest'] != digest:
                    raise OrderError('已处理回执的内容不同，拒绝覆盖', status_code=409)
                saved=json.loads(previous['summary_json'])
                if private_result:
                    return json.loads(orders.cipher.decrypt(saved['encrypted_result'].encode()))
                return saved
            private = copy(orders)
            private._lock = threading.RLock()
            private._connect = lambda: _BorrowedConnection(connection)
            summary = apply(private)
            saved=({'encrypted_result':orders.cipher.encrypt(json.dumps(summary,ensure_ascii=False).encode()).decode()}
                   if private_result else summary)
            connection.execute('INSERT INTO erp_operation_receipts VALUES (?,?,?)',
                               (receipt_key, digest, json.dumps(saved, ensure_ascii=False)))
            connection.commit()
            return summary
        except BaseException:
            connection.rollback()
            raise
