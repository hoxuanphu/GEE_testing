"""Durable download job endpoints and leased, Range-capable completed artifacts."""
from fastapi import HTTPException
from fastapi.responses import FileResponse,JSONResponse
from pydantic import BaseModel,Field

from .download_store import SelectionExpired,SelectionMismatch,AttemptBusy,StaleAttempt


class DownloadJobRequest(BaseModel):
    selection_token: str=Field(min_length=1,max_length=128)


class ArtifactResponse(FileResponse):
    def __init__(self,store,artifact_key,filename):
        self.store,self.artifact_key=store,artifact_key
        # Path is resolved under a lease immediately before stat/first byte.
        super().__init__(path=store.root/'not-published',media_type='image/tiff',filename=filename)

    async def __call__(self,scope,receive,send):
        try:
            with self.store.artifact_lease(self.artifact_key) as receipt:
                self.path=receipt.filename
                self.headers['etag']='"'+receipt.sha256+'"'
                # FileResponse supports HEAD, If-Range and Range/206 and uses
                # bounded reads; the finally in artifact_lease covers disconnect.
                await super().__call__(scope,receive,send)
        except FileNotFoundError:
            await JSONResponse({'detail':'Tệp kết quả đã hết hạn.'},status_code=410)(scope,receive,send)


def register_download_routes(app,service_provider,enabled):
    def invoke(function,*args):
        try:
            return function(*args)
        except SelectionExpired as exc:
            raise HTTPException(410,str(exc)) from None
        except (SelectionMismatch,AttemptBusy,StaleAttempt,ValueError) as exc:
            raise HTTPException(409,str(exc)) from None
        except LookupError:
            raise HTTPException(404,'Không tìm thấy lượt tải.') from None

    @app.post('/api/download-jobs',status_code=202)
    def start_download_job(body:DownloadJobRequest):
        if not enabled():
            raise HTTPException(404,'Luồng tải nền chưa được bật.')
        return invoke(service_provider().start,body.selection_token)

    @app.get('/api/download-jobs/{job_id}')
    def download_job_status(job_id:str):
        return invoke(service_provider().status,job_id)

    for operation in ('pause','resume','cancel'):
        def make_handler(action):
            def handler(job_id:str):
                return invoke(getattr(service_provider(),action),job_id)
            return handler
        app.add_api_route('/api/download-jobs/{job_id}/'+operation,make_handler(operation),
                          methods=['POST'],name='download_job_'+operation)

    @app.api_route('/api/download-jobs/{job_id}/file',methods=['GET','HEAD'])
    def download_job_file(job_id:str):
        service=service_provider()
        job=invoke(service.store.get_job,job_id)
        if job['state']!='completed':
            raise HTTPException(409,'Ảnh chưa hoàn tất xử lý.')
        return ArtifactResponse(service.store,job['artifact_key'],f'sentinel2_{job_id[:8]}.tif')
