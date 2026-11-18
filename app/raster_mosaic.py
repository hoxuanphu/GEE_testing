"""Virtual assembly of transport pieces of one GEE image, preserving priority."""
from contextlib import ExitStack
import math
from pathlib import Path
import tempfile
from xml.etree import ElementTree as ET

import rasterio
from rasterio.transform import from_origin

from .imagery import _mosaic_placements, _read_mosaic_window
from .raster_grid import atomic_raster_path,grid_profile,write_grid_blocks


def _element(parent,name,text=None,**attributes):
    child=ET.SubElement(parent,name,{key:str(value) for key,value in attributes.items()})
    if text is not None:
        child.text=str(text)
    return child


def build_transport_vrt(receipts,directory):
    """Band→ordered paths mapping. Later GDAL sources must be legacy first-valid."""
    directory=Path(directory)
    directory.mkdir(parents=True,exist_ok=True)
    root=None
    reference_grid=None
    for band_index,(band,paths) in enumerate(receipts.items(),1):
        with ExitStack() as stack:
            sources=[stack.enter_context(rasterio.open(path)) for path in paths]
            if not sources:
                raise ValueError('No transport tiles')
            ref=sources[0]
            if any(s.crs!=ref.crs or s.transform.b or s.transform.d or s.transform.a<=0 or s.transform.e>=0 for s in sources):
                raise ValueError('Transport tiles must share a north-up CRS')
            profile=ref.profile.copy()
            if len(sources)>1:
                west=min(s.bounds.left for s in sources)
                south=min(s.bounds.bottom for s in sources)
                east=max(s.bounds.right for s in sources)
                north=max(s.bounds.top for s in sources)
                profile.update(width=int(round((east-west)/ref.res[0])),height=int(round((north-south)/ref.res[1])),
                               transform=from_origin(west,north,*ref.res))
            grid=(ref.crs,profile['transform'],profile['width'],profile['height'])
            if root is None:
                reference_grid=grid
                root=ET.Element('VRTDataset',rasterXSize=str(grid[2]),rasterYSize=str(grid[3]))
                _element(root,'SRS',ref.crs.to_wkt())
                _element(root,'GeoTransform',','.join(str(x) for x in profile['transform'].to_gdal()))
                metadata=_element(root,'Metadata')
                for key,value in ref.tags().items():
                    _element(metadata,'MDI',value,key=key)
            elif grid!=reference_grid:
                raise ValueError('Transport band grids differ')
            output=_element(root,'VRTRasterBand',dataType='Float32',band=band_index)
            _element(output,'Description',band)
            _element(output,'NoDataValue','nan')
            _element(output,'Scale',ref.scales[0])
            _element(output,'Offset',ref.offsets[0])
            if ref.units[0]:
                _element(output,'UnitType',ref.units[0])
            metadata=_element(output,'Metadata')
            for key,value in ref.tags(1).items():
                _element(metadata,'MDI',value,key=key)
            for source_index,(source,source_window,target) in enumerate(reversed(_mosaic_placements(sources,profile))):
                # A VRT ComplexSource's mask takes precedence over NODATA.
                # Normalize native -9999/mask into NaN in a tiny XML wrapper,
                # so the outer source has a single unambiguous validity rule.
                normalized=ET.Element('VRTDataset',rasterXSize=str(source.width),rasterYSize=str(source.height))
                _element(normalized,'SRS',source.crs.to_wkt())
                _element(normalized,'GeoTransform',','.join(str(x) for x in source.transform.to_gdal()))
                normalized_band=_element(normalized,'VRTRasterBand',dataType='Float32',band=1)
                _element(normalized_band,'NoDataValue','nan')
                normalized_source=_element(normalized_band,'ComplexSource')
                _element(normalized_source,'SourceFilename',str(Path(source.name).resolve()),relativeToVRT=0)
                _element(normalized_source,'SourceBand',1)
                _element(normalized_source,'UseMaskBand','true')
                normalized_path=directory/f'normalized-{band_index}-{source_index}.vrt'
                ET.ElementTree(normalized).write(normalized_path,encoding='utf-8',xml_declaration=True)
                item=_element(output,'ComplexSource',resampling='nearest')
                _element(item,'SourceFilename',str(normalized_path.resolve()),relativeToVRT=0)
                _element(item,'SourceBand',1)
                _element(item,'SrcRect',xOff=source_window.col_off,yOff=source_window.row_off,
                         xSize=source_window.width,ySize=source_window.height)
                _element(item,'DstRect',xOff=target.col_off,yOff=target.row_off,xSize=target.width,ySize=target.height)
                _element(item,'NODATA','nan')
                _element(item,'UseMaskBand','true')
    if root is None:
        raise ValueError('No transport bands')
    path=directory/'transport.vrt'
    ET.ElementTree(root).write(path,encoding='utf-8',xml_declaration=True)
    return path


def assemble_to_grid(receipts,output,grid,*,categorical_flags=False,check=lambda:None,metrics=None,on_block=lambda:None):
    check()
    names=list(receipts)
    flags=[categorical_flags]*len(names) if isinstance(categorical_flags,bool) else list(categorical_flags)
    if categorical_flags is False:
        from .imagery import S2_CATEGORICAL_BANDS
        flags=[name in S2_CATEGORICAL_BANDS for name in names]
    if not names or len(flags)!=len(names):
        raise ValueError('One categorical flag is required per band')
    categorical_only=all(flags)
    dtype='uint8' if categorical_only else 'float32'
    fill=255 if categorical_only else float('nan')
    with tempfile.TemporaryDirectory(prefix='transport-vrt-',dir=Path(output).parent) as directory, \
         atomic_raster_path(output) as temporary:
        with rasterio.open(temporary,'w',**grid_profile(grid,len(names),dtype,fill)) as destination:
            vrt=build_transport_vrt(receipts,Path(directory))
            with rasterio.open(vrt) as source:
                write_grid_blocks(source,destination,grid,flags,check=check,on_block=on_block)
                for index,name in enumerate(names,1):
                    destination.set_band_description(index,name)
                    destination.update_tags(index,**source.tags(index))
                destination.update_tags(**source.tags())
                destination.scales,destination.offsets,destination.units=source.scales,source.offsets,source.units
    check()
