#!/usr/bin/env python3
from __future__ import annotations
from os import PathLike

from typing import Any, NamedTuple, Optional, List, Tuple, Set, Dict, TypeVar

import argparse
import json
import logging
import pathlib
import re
import shutil
import sys
import tempfile


logging.basicConfig(stream=sys.stderr)

HEADER_MARKER = "; Pre-Processed for Cancel-Object support\n"


PathLike = TypeVar("PathLike", str, pathlib.Path)


class Point(NamedTuple):
    x: float
    y: float


class HullTracker:
    def __init__(self):
        self.pos = None
        self.points: Set[Point] = set()

    def add_point(self, point: Point):
        self.points.add(point)

    def center(self):
        if self.points:
            x = sum(p[0] for p in self.points)
            y = sum(p[1] for p in self.points)
            return x / len(self.points), y / len(self.points)

    def exterior(self):
        if self.points:
            points = iter(self.points)
            first = next(points)
            min_x = max_x = first[0]
            min_y = max_y = first[1]

            for (x, y) in points:
                if x < min_x:
                    min_x = x
                if x > max_x:
                    max_x = x
                if y < min_y:
                    min_y = y
                if y > max_y:
                    max_y = y

            return boundingbox((min_x, min_y), (max_x, max_y))


class KnownObject(NamedTuple):
    name: str
    hull: HullTracker


def boundingbox(pmin: Point, pmax: Point):
    return [
        (pmin[0], pmin[1]),
        (pmin[0], pmax[1]),
        (pmax[0], pmax[1]),
        (pmax[0], pmin[1]),
    ]


def _dump_coords(coords: List[float]) -> str:
    return ",".join(map(str, coords))


def _clean_id(id):
    return re.sub(r"\W+", "_", id).strip("_")


def parse_gcode(line):
    command, *params = line.strip().split()
    params = {p[0].upper(): p[1:] for p in params}
    return command, params


def header(object_count):
    yield "\n\n"
    yield HEADER_MARKER
    yield f"; {object_count} known objects\n"


def define_object(
    name,
    center: Optional[Point] = None,
    polygon: Optional[Point] = None,
    region: Optional[List[Point]] = None,
):
    yield f"DEFINE_OBJECT NAME={name}"
    if center:
        yield f" CENTER={_dump_coords(center)}"
    if polygon:
        yield f" POLYGON={json.dumps(polygon, separators=(',', ':'))}"
    if region:
        yield f" REGION={_dump_coords(region, separators=(',', ':'))}"
    yield "\n"


def object_start_marker(object_name):
    yield f"START_CURRENT_OBJECT NAME={object_name}\n"


def object_end_marker(object_name):
    yield f"END_CURRENT_OBJECT NAME={object_name}\n"


def preprocess_pipe(infile):
    yield from infile

def preprocess_cura(infile):
    known_objects: Dict[str, KnownObject] = {}
    current_hull: Optional[HullTracker] = None
    last_time_elapsed: str = None

    # iterate the file twice, to be able to inject the header markers
    for line in infile:
        if line.startswith(";MESH:"):
            object_name = line.split(":", maxsplit=1)[1].strip()
            if object_name == "NONMESH":
                continue
            if object_name not in known_objects:
                known_objects[object_name] = KnownObject(_clean_id(object_name), HullTracker())
            current_hull = known_objects[object_name].hull

        if current_hull and line.strip().lower().startswith("g"):
            command, params = parse_gcode(line)
            if "E" in params and "X" in params and "Y" in params:
                x = float(params["X"])
                y = float(params["Y"])
                current_hull.add_point(Point(x, y))

        if line.startswith(";TIME_ELAPSED:"):
            last_time_elapsed = line

    infile.seek(0)
    for line in infile:
        yield line
        if line.strip() and not line.startswith(";"):
            break

    # Inject custom marker
    yield from header(len(known_objects))
    for mesh_id, hull in known_objects.values():
        yield from define_object(
            mesh_id,
            center=hull.center(),
            polygon=hull.exterior(),
        )

    current_object = None
    for line in infile:
        yield line

        if line.startswith(";MESH:"):
            if current_object:
                yield from object_end_marker(current_object)
                current_object = None
            mesh = line.split(":", maxsplit=1)[1].strip()
            if mesh == "NONMESH":
                continue
            current_object, _ = known_objects[mesh]
            yield from object_start_marker(current_object)

        if line == last_time_elapsed and current_object:
            yield from object_end_marker(current_object)
            current_object = None

    if current_object:
        yield from object_end_marker(current_object)


def preprocess_superslicer(infile):
    known_objects: Dict[str, Dict[str, Any]] = {}

    for line in infile:
        yield line

        # ; object: {
        #     "name": "cube_1",
        #     "id": "cube_1 id:0 copy 0",
        #     "object_center": [150.505357,155.500000,0.000000],
        #     "boundingbox_center":[150.505357,155.500000,2.500000],
        #     "boundingbox_size":[5.000000,5.000000,5.000000]
        #   }

        if line.startswith("; object:"):
            object_data = json.loads(line.split(":", maxsplit=1)[1].strip())
            object_data["clean_id"] = _clean_id(object_data["id"])
            known_objects[object_data["id"]] = object_data

        if line.startswith("; plater:"):
            # Done. Header time
            yield from header(len(known_objects))
            for object_data in known_objects.values():
                polygon = None
                boundingbox_center = object_data.get("boundingbox_center", None)
                boundingbox_size = object_data.get("boundingbox_size", None)
                if boundingbox_center and boundingbox_size:
                    [x, y, *_] = boundingbox_center
                    [w, h, *_] = boundingbox_size
                    polygon = boundingbox((x - w / 2, y - h / 2), (x + w / 2, y + h / 2))

                yield from define_object(
                    object_data["clean_id"],
                    center=object_data.get("object_center"),
                    polygon=polygon,
                )

            break

    for line in infile:
        yield line

        if line.startswith("; printing object "):
            yield from object_start_marker(known_objects[line.split("printing object")[1].strip()]["clean_id"])

        if line.startswith("; stop printing object "):
            yield from object_end_marker(known_objects[line.split("printing object")[1].strip()]["clean_id"])


def preprocess_slicer(infile):
    known_objects: Dict[str, KnownObject] = {}
    current_hull: Optional[HullTracker] = None
    for line in infile:
        if line.startswith("; printing object "):
            object_id = line.split("printing object")[1].strip()
            if object_id not in known_objects:
                known_objects[object_id] = KnownObject(_clean_id(object_id), HullTracker())
            current_hull = known_objects[object_id].hull

        if current_hull and line.strip().lower().startswith("g"):
            command, params = parse_gcode(line)
            if "E" in params and "X" in params and "Y" in params:
                x = float(params["X"])
                y = float(params["Y"])
                current_hull.add_point(Point(x, y))

    infile.seek(0)

    for line in infile:
        yield line

        if line.startswith("; generated by"):
            yield from header(len(known_objects))
            for object_id, hull in known_objects.values():
                yield from define_object(
                    object_id,
                    center=hull.center(),
                    polygon=hull.exterior(),
                )

        if line.startswith("; printing object "):
            yield from object_start_marker(known_objects[line.split("printing object")[1].strip()].name)

        if line.startswith("; stop printing object "):
            yield from object_end_marker(known_objects[line.split("printing object")[1].strip()].name)


def preprocess_ideamaker(infile):
    # This one is funnier
    # theres blocks like this, we can grab all these to get the names and ideamaker's IDs for them.
    #   ;PRINTING: test_bed_part0.3mf
    #   ;PRINTING_ID: 0

    known_objects: Dict[str, KnownObject] = {}
    current_hull: HullTracker = None

    for line in infile:
        if line.startswith(";PRINTING:"):
            name = line.split(":")[1].strip()
            id_line = next(infile)
            assert id_line.startswith(";PRINTING_ID:")
            id = id_line.split(":")[1].strip()
            # Ignore the internal non-object meshes
            if id == "-1":
                continue
            if id not in known_objects:
                known_objects[id] = KnownObject(_clean_id(name), HullTracker())
            current_hull = known_objects[id].hull

        if current_hull and line.strip().lower().startswith("g"):
            command, params = parse_gcode(line)
            if "E" in params and "X" in params and "Y" in params:
                x = float(params["X"])
                y = float(params["Y"])
                current_hull.add_point(Point(x, y))

    infile.seek(0)

    current_object: Optional[KnownObject] = None
    for line in infile:
        yield line

        if line.startswith(";TOTAL_NUM:"):
            total_num = int(line.split(":")[1].strip())
            assert total_num == len(known_objects)
            yield from header(total_num)
            for id, (name, hull) in known_objects.items():
                yield from define_object(
                    name,
                    center=hull.center(),
                    polygon=hull.exterior(),
                )

        if line.startswith(";PRINTING_ID:"):
            printing_id = line.split(":")[1].strip()
            if current_object:
                yield from object_end_marker(current_object.name)
                current_object = None
            if printing_id == "-1":
                continue
            current_object = known_objects[printing_id]
            yield from object_start_marker(current_object.name)

        if line == ";REMAINING_TIME: 0\n" and current_object:
            yield from object_end_marker(current_object.name)
            current_object = None

    if current_object:
        yield from object_end_marker(current_object.name)


# Note:
#   Slic3r does not output any markers into GCode
#   Kisslicer does not output any markers into GCode
SLICERS: dict[str, Tuple[str, callable]] = {
    "superslicer": ("; generated by SuperSlicer", preprocess_superslicer),
    "prusaslicer": ("; generated by PrusaSlicer", preprocess_slicer),
    "slic3r": ("; generated by Slic3r", preprocess_slicer),
    "cura": (";Generated with Cura_SteamEngine", preprocess_cura),
    "ideamaker": (";Sliced by ideaMaker", preprocess_ideamaker),
    # "simplify3d": ("", preprocess_simplify3d),
}


def identify_slicer_marker(line):
    for name, (marker, processor) in SLICERS.items():
        if line.strip().startswith(marker):
            logging.debug('Identified slicer {}', name)
            return processor


def preprocessor(infile, outfile):
    for line in infile:
        if line.startswith("DEFINE_OBJECT"):
            logging.info('GCode already supports cancellation')
            infile.seek(0)
            outfile.write(infile.read())
            return True

    processor = None
    infile.seek(0)
    for line in infile:
        if not line.strip():
            continue

        if not line.startswith(";"):
            logging.error("Error, reached the end of the comments without finding a slicer marker")
            return

        if not processor:
            processor = identify_slicer_marker(line)
            if processor:
                break

    infile.seek(0)
    for line in processor(infile):
        outfile.write(line)

    return True

def process_file_for_cancellation(filename:PathLike, output_suffix=None) -> int:
    filepath = pathlib.Path(filename)
    outfilepath = filepath

    if output_suffix:
        outfilepath = outfilepath.with_name(outfilepath.stem + output_suffix + outfilepath.suffix)

    tempfilepath = pathlib.Path(tempfile.mktemp())

    with filepath.open("r") as fin:
        with tempfilepath.open("w") as fout:
            res = preprocessor(fin, fout)

    if res:
        if outfilepath.exists():
            outfilepath.unlink()
        shutil.move(tempfilepath, outfilepath)

    else:
        tempfilepath.unlink()

    return res

if __name__ == "__main__":
    argparser = argparse.ArgumentParser()
    argparser.add_argument(
        "--output-suffix", "-o", help="Add a suffix to gcoode output. Without this, gcode will be rewritten in place"
    )
    # argparser.add_argument("--stdout", help="Write the processed gcode to stdout. Only works for a single input file")
    argparser.add_argument("gcode", nargs="*")

    exitcode = 0

    args = argparser.parse_args()

    for filename in args.gcode:
        if not process_file_for_cancellation(filename, args.output_suffix):
            exitcode = 1

    sys.exit(exitcode)
