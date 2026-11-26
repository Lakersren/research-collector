"""Download article images, save to local disk, checkpoint retry URLs locally."""
import hashlib
import logging
import mimetypes
from aris.observability import log_context
from aris.web_media import SiteStore
from corpus_contract import image_ext
from aris.paths import RAW

log = logging.getLogger('corpus.images')

def sync_images(source, objects):
    from collectors.web_media_dl import wanted, try_one
    from collectors.sites import nvidia
    if source['_seg']=='semi':
        from collectors.semianalysis_media import wanted as semi_wanted
        need=semi_wanted()
    else:
        need=wanted(SiteStore(source['key']))
    store=SiteStore(source['key'])
    ledger=store.load_ledger()
    failed=0
    uploaded=reused=0
    log.info('image_sync_started images=%s', len(need))
    for key, req in need.items():
        row=ledger.get(key,{})
        try:
            if row.get('status')=='ok' and row.get('file_key') and objects.exists(row['file_key']):
                reused+=1
                continue
            body=None
            if row.get('file'):
                p=(RAW/row['file']).resolve()
                if p.is_relative_to(RAW.resolve()) and p.is_file():
                    body=p.read_bytes()
            ext = image_ext(body) if body else None
            candidates = req['cands']
            if source['key'] == 'web:nvidia_dev':
                candidates = list(dict.fromkeys(url for original in candidates
                                                for url in nvidia.image_candidates(original)))
            if not ext:
                for url in candidates:
                    try:
                        body,_,_=try_one(url,source.get('host') or source.get('feed') or key,30)
                        ext = image_ext(body)
                        if not ext:
                            raise ValueError('响应不是支持的图片')
                        break
                    except Exception:
                        log.warning('image_download_candidate_failed image_ref=%s',
                                    hashlib.sha256(key.encode()).hexdigest()[:16], exc_info=True)
                        body=None
                if not body:
                    raise RuntimeError('所有图片地址获取失败')
            file_key=objects.key(source['key'], key, ext)
            if not objects.exists(file_key):
                with log_context(image_ref=hashlib.sha256(key.encode()).hexdigest()[:16], stage='image_upload'):
                    objects.upload(file_key,body,mimetypes.guess_type('image.'+ext)[0] or 'application/octet-stream')
                uploaded+=1
                log.info('image_uploaded file_key=%s bytes=%s', file_key, len(body))
            ledger[key]={**row,'key':key,'candidates':req['cands'],'file_key':file_key,
                         'file':file_key,'status':'ok','w':req.get('w'),'h':req.get('h')}
        except Exception as e:
            failed+=1
            with log_context(image_ref=hashlib.sha256(key.encode()).hexdigest()[:16]):
                log.exception('image_sync_failed')
            # Do not persist credentials or signed request details from SDK exceptions.
            ledger[key]={**row,'key':key,'candidates':req['cands'],'file_key':None,
                         'status':'failed','error':type(e).__name__,'w':req.get('w'),'h':req.get('h')}
        store.save_ledger(ledger)
    log.info('image_sync_finished total=%s uploaded=%s reused=%s failed=%s', len(need), uploaded, reused, failed)
    return failed
