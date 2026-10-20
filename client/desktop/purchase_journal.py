"""Durable, encrypted same-task supplier write records for procurement."""
import hashlib
import json
import os
from pathlib import Path
import uuid

from erp_desktop import protect


class PurchaseOutcomeUnknown(RuntimeError):
    outcome_unknown = True
    code = 'purchase_write_outcome_unknown'


def _canonical(value):
    if isinstance(value, bytes):
        return {'bytes_sha256':hashlib.sha256(value).hexdigest(),'size':len(value)}
    if isinstance(value, dict):return {k:_canonical(v) for k,v in value.items()}
    if isinstance(value, (list,tuple)):return [_canonical(v) for v in value]
    return value


def _save(path, value):
    content=protect(json.dumps(value,ensure_ascii=False).encode('utf-8'))
    temporary=path.with_name(uuid.uuid4().hex+'.partial')
    with temporary.open('xb') as stream:
        stream.write(content);stream.flush();os.fsync(stream.fileno())
    os.replace(temporary,path)


async def write_once(directory: Path, operation: str, arguments, invoke, check):
    """Caller supplies a stable task/group/stage key, never a new retry ID.

    The write intent is flushed before calling the supplier. A pending record
    stops recovery until the original external ID is reconciled. Only a done
    record may replay its result inside the same task.
    """
    if not isinstance(operation,str) or not operation or len(operation)>500:
        raise ValueError('采购操作编号无效')
    directory.mkdir(parents=True,exist_ok=True)
    check()
    lock=directory/(hashlib.sha256(operation.encode()).hexdigest()+'.lock')
    with lock.open('a+b') as handle:
        if handle.tell()==0:handle.write(b'0');handle.flush()
        handle.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            raise PurchaseOutcomeUnknown('同一采购操作仍在执行，禁止重复提交') from None
        try:
            return await _write_locked(directory,operation,arguments,invoke,check)
        finally:
            handle.seek(0)
            if os.name=='nt':msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(handle.fileno(),fcntl.LOCK_UN)


async def _write_locked(directory, operation, arguments, invoke, check):
    path=directory/(hashlib.sha256(operation.encode()).hexdigest()+'.dpapi')
    digest=hashlib.sha256(json.dumps(_canonical(arguments),sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    if path.exists():
        previous=json.loads(protect(path.read_bytes(),decrypt=True))
        if previous.get('digest')!=digest:
            raise PurchaseOutcomeUnknown('原采购操作参数已变化，请核对已有结果')
        if previous.get('state')=='done':return previous['result']
        raise PurchaseOutcomeUnknown('采购写入结果未知，请先核对原外部订单号，禁止重复提交')
    _save(path,{'state':'pending','digest':digest})
    try:
        result=await invoke()
    except Exception as error:
        # Do not retain supplier exception text: it may include addresses/tokens.
        raise PurchaseOutcomeUnknown('采购写入未确认，请核对原外部订单号') from error
    _save(path,{'state':'done','digest':digest,'result':result})
    # A lease failure after the remote write cannot discard the saved result.
    check()
    return result
