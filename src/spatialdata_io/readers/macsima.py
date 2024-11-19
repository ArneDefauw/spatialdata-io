from __future__ import annotations

import glob
import re
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

import dask.array as da
import spatialdata as sd
from bioio import BioImage
from ome_types.model import Pixels, UnitsLength
from spatialdata import SpatialData
from spatialdata._logging import logger
from spatialdata.transformations import Identity

from spatialdata_io._constants._constants import MacsimaKeys
from spatialdata_io._docs import inject_docs

__all__ = ["macsima"]


@inject_docs(vx=MacsimaKeys)
def macsima(
    path: str | Path,
    c_subset: list[str] = None,
    transformations: bool = False,
    imread_kwargs: Mapping[str, Any] = MappingProxyType({}),
    image_models_kwargs: Mapping[str, Any] = MappingProxyType({}),
) -> SpatialData:
    """
    Read *MACSima* formatted dataset.

    This function reads images from a MACSima cyclic imaging experiment.

    .. seealso::

        - `MACSima output <https://application.qitissue.com/getting-started/naming-your-datasets>`_.

    Parameters
    ----------
    path
        Path to the directory containing the data.
    c_subset
        Channel names to consider.
    transformations
        Whether to add a transformation from pixels to microns to the image.
    imread_kwargs
        Keyword arguments passed to :func:`bioio.BioImage`.
    image_model_kwargs
        Keyword arguments to pass to the image models.

    Returns
    -------
    :class:`spatialdata.SpatialData`
    """
    path_list=glob.glob( f"{path}/*{MacsimaKeys.IMAGE_OMETIF}" )
    if not path_list:
        raise ValueError( f"Cannot determine data set, expecting '*{MacsimaKeys.IMAGE_OMETIF}' files in {path}." )
    imgs=[BioImage(img_path, **imread_kwargs) for img_path in path_list]

    image_name = imgs[0].ome_metadata.experiments[0].description

    metadata =[_get_metadata( _img ) for _img in imgs]
    c_coords = [ item[2] for item in metadata ]
    roi = [ item[3] for item in metadata ]
    if roi[0] is not None:
        assert all( x == roi[0] for x in roi ), f"Extracted ROI ID not equal for all '{MacsimaKeys.IMAGE_OMETIF}' files found in '{path}'."
        roi_id = roi[0]
        to_coordinate_system = f"global_{roi_id}"
        image_name = f"{image_name}_{roi_id}"
    else:
        to_coordinate_system = "global"

    names = [  "_".join([part for part in name_parts if part]) for name_parts in metadata  ] 
    number_pattern = re.compile(r'^\d+')
    # sort by cycle number
    combined_sorted = sorted(list(zip(names, c_coords, imgs)), key=lambda x: int(number_pattern.match(x[0]).group()))
    names, c_coords, imgs = zip(*combined_sorted)

    if c_subset:
        names, c_coords, imgs = map( list, zip(*[
            (elem1, elem2, elem3) for elem1, elem2, elem3 in zip(names, c_coords, imgs)
            if elem2 in c_subset
        ]) )
    
    if not c_coords:
        raise ValueError( f"List of channels to consider is empty after subsetting by {c_subset}" )

    # get physical units:
    pixels_to_microns=_parse_physical_size( pixels=imgs[0].ome_metadata.images[0].pixels )
    # sanity check (physical size of all images should be the same for one ROI)
    for _img in imgs:
        assert pixels_to_microns == _parse_physical_size( _img.ome_metadata.images[0].pixels )
    assert imgs[0].dims.order == "TCZYX"
    array=[_img.get_image_dask_data().squeeze( ) for _img in imgs]
    array=da.stack( array, axis=0 )

    t_pixels_to_microns = sd.transformations.Scale([pixels_to_microns, pixels_to_microns], axes=("x", "y")) if transformations else Identity()

    se = sd.models.Image2DModel.parse(
        array,
        dims=["c", "y", "x"],
        c_coords=names,
        transformations={ to_coordinate_system: t_pixels_to_microns, },
        **image_models_kwargs,
    )

    # spatialdata only allows alphanumeric and _ in the name
    image_name = _clean_string( image_name )
    sdata = sd.SpatialData(images={image_name: se}, table=None)

    return sdata


def _get_structured_annotations( img: BioImage, metadata_key:str  )->str | None:
    structured_annotations = img.ome_metadata.structured_annotations
    value=[item.value[ metadata_key ]  for item in structured_annotations if item.value[ metadata_key ] is not None ]
    if not value:
        return None
    assert all( x == value[0] for x in value ), f"Structured annotations for key '{metadata_key}' are not equal (found '{value}') for object of type '{type(img).__name__}': {img}."
    return value[0]


def _get_metadata( img: BioImage )->list[str|None]:
    cycle=_get_structured_annotations( img, metadata_key=MacsimaKeys.CYCLE )
    # check that cycle is not None
    assert cycle is not None, f"'{MacsimaKeys.CYCLE}' could not be found in metadata for object of type '{type(img).__name__}': {img}"
    # we allow scantype to be None (i.e. not found in structured annotations of ome metadata)
    scantype=_get_structured_annotations( img, metadata_key=MacsimaKeys.SCANTYPE )
    channel_name = getattr(img, MacsimaKeys.CHANNEL_NAMES, None)
    if channel_name is None:
        raise AttributeError(f"Attribute '{MacsimaKeys.CHANNEL_NAMES}' not found for object of type '{type(img).__name__}': {img}")
    assert len( channel_name ) ==1, f"There should be exactly one channel specified in metadata, but found '{channel_name}'."
    channel_name =channel_name[0]
    if not channel_name:
        raise ValueError( f"'{MacsimaKeys.CHANNEL_NAMES}' is not specified in metadata for object of type '{type(img).__name__}': {img}" )
    # get the reagents used from ome metadata if they can be found in ome metadata
    reagents=None
    if hasattr( img.ome_metadata, MacsimaKeys.SCREENS ):
        screens=getattr( img.ome_metadata, MacsimaKeys.SCREENS )
        assert len( screens ) == 1, f"There should be exactly one '{MacsimaKeys.SCREENS}' specified in ome metadata, but found '{screens}' for object of type '{type(img).__name__}': {img}."
        screens = screens[0]
        reagents=getattr( screens, MacsimaKeys.REAGENTS, None )
        if reagents:
            assert len( reagents ) == 1, f"There should be exactly one '{MacsimaKeys.REAGENTS}' specified in ome metadata, but found '{reagents}' for object of type '{type(img).__name__}': {img}."
            reagents = reagents[0].name
    roi_id=_get_structured_annotations( img, metadata_key=MacsimaKeys.ROI_ID ) or _get_structured_annotations( img, metadata_key=MacsimaKeys.ROI_ID_deprecated )
    return [ cycle, scantype, channel_name, roi_id, reagents ]


def _parse_physical_size(pixels: Pixels | None = None) -> float:
    """Parse physical size from OME-TIFF to micrometer."""
    logger.debug(pixels)
    if pixels.physical_size_x_unit != pixels.physical_size_y_unit:
        logger.error("Physical units for x and y dimensions are not the same.")
        raise NotImplementedError
    if pixels.physical_size_x != pixels.physical_size_y:
        logger.error("Physical sizes for x and y dimensions are the same.")
        raise NotImplementedError
    # convert to micrometer if needed
    if pixels.physical_size_x_unit == UnitsLength.NANOMETER:
        physical_size = pixels.physical_size_x / 1000
    elif pixels.physical_size_x_unit == UnitsLength.MICROMETER:
        physical_size = pixels.physical_size_x
    else:
        logger.error(f"Physical unit not recognized: '{pixels.physical_size_x_unit}'.")
        raise NotImplementedError
    return float(physical_size)

def _clean_string(input_string:str)->str:
    """Replace all non-alphanumeric characters with '_'"""
    output_string = re.sub(r'[^a-zA-Z0-9_]', '_', input_string)
    return output_string
