"""Cloud read transport: prepared thumbnails and managed OSS originals, no business jobs."""
import re
from urllib.parse import urlsplit
from fastapi.responses import FileResponse, RedirectResponse
from erp.core.config import get_settings
from erp.core.errors import AppError, NotFoundError
from erp.domains.pod.models import PodArtwork

def cloud_artwork_response(session, artwork_id: str, kind: str):
    item=session.get(PodArtwork,artwork_id)
    if not item:raise NotFoundError('图库图片不存在')
    if item.asset.mirror_status=='deleted':
        raise AppError('该资产已删除',code='asset_deleted',status_code=410)
    if kind=='thumbnail':
        if not re.fullmatch(r'[a-fA-F0-9]{64}',item.sha256):
            raise AppError('缩略图索引无效',code='thumbnail_invalid',status_code=409)
        root=(get_settings().data_dir/'cloud-thumbnails').resolve()
        path=root/(item.sha256.lower()+'.jpg')
        if path.is_symlink() or path.resolve().parent!=root or not path.is_file():
            raise AppError('云端缩略图尚未同步',code='cloud_thumbnail_missing',status_code=404)
        return FileResponse(path,media_type='image/jpeg')
    if kind=='file':
        url=item.asset.oss_url or ''
        try:
            parsed=urlsplit(url)
            valid=(parsed.scheme=='https' and parsed.hostname=='example-assets.oss-cn-chengdu.aliyuncs.com'
                   and parsed.port in (None,443) and not parsed.username and not parsed.password
                   and not parsed.query and not parsed.fragment and '\\' not in url
                   and not any(ord(c)<32 for c in url))
        except ValueError:valid=False
        if not valid:raise AppError('原图尚未归档至允许的 OSS',code='asset_remote_forbidden',status_code=403)
        return RedirectResponse(url,status_code=307)
    raise AppError('该云端图片操作尚未开放',code='pilot_scope_only',status_code=403)
