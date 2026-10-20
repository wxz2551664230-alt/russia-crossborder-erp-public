from fastapi import APIRouter,Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session
from erp.core.database import get_db
from erp.services import codex_handoffs as service

router=APIRouter(prefix='/codex-handoffs',tags=['codex-handoffs'])

@router.get('/batches/{batch_id}')
def bundle(batch_id:str,session:Session=Depends(get_db)):
    return service.bundle(session,batch_id)

@router.get('/{job_id}/local-context')
def context(job_id:str,worker_id:str,session:Session=Depends(get_db)):
    return service.context(session,job_id,worker_id)

class Resume(BaseModel):
    worker_id:str
    receipts:dict

@router.post('/{job_id}/resume')
def resume(job_id:str,request:Resume,session:Session=Depends(get_db)):
    return service.resume(session,job_id,request.worker_id,request.receipts)
