"""Fast submission and year-specific artifacts; no provider enumeration on HTTP submit."""
from copy import deepcopy

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import ConfigDict

from .timeseries import BusyError
from .vector_api import vector_area


def batch_parameters(body):
    from . import main
    parameters = body.model_dump()
    rectangle = None
    if body.selection_mode == 'rectangle':
        rectangle = main._rectangle_model(body.center_lon,body.center_lat,body.width_m,body.height_m,body.rotation_deg)
        geometry, bbox = rectangle.model_geometry()
    elif body.selection_mode == 'vector':
        area = vector_area(main._vector_store,body.vector_roi_id)
        geometry, bbox = area.geometry,area.bbox
    else:
        if body.lon is None or body.lat is None:
            raise HTTPException(422,'Chọn tọa độ tâm trước khi bắt đầu.')
        geometry, bbox = main.point_roi(body.lon,body.lat,body.radius_m,body.rotation_deg)
    parameters.update(main.timeseries_source_metadata(body.dataset, body.polarization, body.s2_auxiliary_bands,
                                                     s2_band_preset=body.s2_band_preset))
    parameters.update(roi_geometry=deepcopy(geometry),bbox=vars(bbox),crs='EPSG:4326')
    if rectangle:
        parameters['rectangle'] = rectangle.model_dump()
        parameters['output_grid'] = rectangle.output_grid(parameters['scale_m'])
    else:
        for field in ('center_lon','center_lat','width_m','height_m'):
            parameters.pop(field,None)
        if body.selection_mode=='point' and body.rotation_deg:
            parameters['output_grid'] = main.build_rotated_grid(body.lon,body.lat,body.radius_m,body.rotation_deg,parameters['scale_m'])
    return parameters


def register_batch_routes(api, store, service):
    from . import main
    router = APIRouter(prefix='/api/timeseries/batches')

    class BatchRequest(main.TimeSeriesRequest):
        model_config = ConfigDict(extra='forbid',allow_inf_nan=False)

    def existing(batch_id):
        try:
            batch = store.get_batch(batch_id)
        except (ValueError,TypeError):
            batch = None
        if batch is None:
            raise HTTPException(404,'Không tìm thấy tác vụ theo năm hoặc tác vụ đã hết hạn.')
        return batch

    def create(body):
        try:
            return service().start(batch_parameters(body))
        except BusyError as exc:
            raise HTTPException(409,str(exc)) from None
        except ValueError:
            raise HTTPException(422,'Tham số tải theo năm không hợp lệ.') from None
        except RuntimeError:
            raise HTTPException(503,'Không thể khởi chạy worker; hãy thử lại.') from None
    create.__annotations__['body'] = BatchRequest
    router.post('')(create)

    @router.get('/{batch_id}')
    def status(batch_id: str):
        return existing(batch_id)

    @router.post('/{batch_id}/pause')
    def pause(batch_id: str):
        existing(batch_id)
        return service().pause(batch_id)

    def continue_batch(batch_id, retry):
        existing(batch_id)
        try:
            return service().resume(batch_id,retry=retry)
        except (BusyError,ValueError) as exc:
            raise HTTPException(409,str(exc)) from None
        except RuntimeError:
            raise HTTPException(503,'Không thể khởi chạy worker; hãy thử lại.') from None

    @router.post('/{batch_id}/resume')
    def resume(batch_id: str):
        return continue_batch(batch_id,False)

    @router.post('/{batch_id}/retry')
    def retry(batch_id: str):
        return continue_batch(batch_id,True)

    def artifact(batch_id, year, kind):
        batch = existing(batch_id)
        if not any(row['year']==year for row in batch['years']):
            raise HTTPException(404,'Năm không thuộc tác vụ này.')
        path = store.artifact(batch_id,year,kind)
        if path is None:
            raise HTTPException(409,'Kết quả năm chưa sẵn sàng.')
        extension = 'zip' if kind=='download' else 'csv'
        filename = f'timeseries_{batch["parameters"]["dataset"]}_{year}.{extension}'
        return FileResponse(path,media_type='application/zip' if kind=='download' else 'text/csv; charset=utf-8',
                            filename=filename,headers={'Cache-Control':'no-store'})

    @router.get('/{batch_id}/years/{year}/download')
    def download(batch_id: str, year: int):
        return artifact(batch_id,year,'download')

    @router.get('/{batch_id}/years/{year}/csv')
    def csv(batch_id: str, year: int):
        return artifact(batch_id,year,'csv')

    api.include_router(router)
