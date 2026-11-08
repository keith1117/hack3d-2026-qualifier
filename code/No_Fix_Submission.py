import argparse
from pathlib import Path
import json
import re
import zipfile
import xml.etree.ElementTree as ET

import numpy as np
import trimesh


ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "inputs"
OUTPUT = ROOT / "cad"


def read_ply(path):
    # Some clue text was added to numeric rows and to end_header.
    lines = path.read_text().splitlines()
    start = next(i + 1 for i, line in enumerate(lines)
                 if line.startswith("end_header"))
    header = "\n".join(lines[:start])
    nv = int(re.search(r"element vertex (\d+)", header).group(1))
    nf = int(re.search(r"element face (\d+)", header).group(1))
    vertices = np.array([[float(x) for x in row.split()[:3]]
                         for row in lines[start:start + nv]])
    faces = np.array([[int(x) for x in row.split()[1:4]]
                      for row in lines[start + nv:start + nv + nf]])
    if len(faces) != nf or faces.min() < 0 or faces.max() >= nv:
        raise ValueError("Invalid face data in " + path.name)
    return vertices, faces


def read_benchy():
    # The official 3MF keeps vertex indices, unlike the downloaded STL.
    with zipfile.ZipFile(INPUT / "3DBenchy.3mf") as archive:
        name = next(n for n in archive.namelist() if n.endswith(".model"))
        root = ET.fromstring(archive.read(name))
    ns = {"m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"}
    mesh = root.find(".//m:mesh", ns)
    vertices = [[float(v.attrib[k]) for k in ("x", "y", "z")]
                for v in mesh.find("m:vertices", ns)]
    faces = [[int(f.attrib[k]) for k in ("v1", "v2", "v3")]
             for f in mesh.find("m:triangles", ns)]
    model = trimesh.Trimesh(vertices, faces, process=False)
    # This also cleans the very thin triangles in the reference mesh.
    return trimesh.boolean.union([model], engine="manifold")


def fit_tower(vertices):
    # Use the longest PCA axis as the cylinder axis.
    centered = vertices - vertices.mean(axis=0)
    _, axes = np.linalg.eigh(np.cov(centered.T))
    aligned = centered @ axes
    radius = np.linalg.norm(aligned[:, :2], axis=1)
    middle = (np.abs(aligned[:, 2]) < 10) & (radius > 5)
    x, y = aligned[middle, :2].T
    a = np.column_stack([2 * x, 2 * y, np.ones(len(x))])
    cx, cy, c = np.linalg.lstsq(a, x * x + y * y, rcond=None)[0]
    diameter = 2 * np.sqrt(c + cx * cx + cy * cy)
    top = np.median(aligned[aligned[:, 2] > 11.5, 2])
    bottom = np.median(aligned[aligned[:, 2] < -11.5, 2])
    return round(float(diameter), 1), round(float(top - bottom), 1)


def restore_tower(diameter, height):
    model = trimesh.creation.cylinder(radius=diameter / 2,
                                     height=height, sections=128)
    model.apply_translation([0, 0, height / 2])
    return model, {"diameter_mm": diameter, "height_mm": height}


def cylinder(radius, length, center, direction="z"):
    model = trimesh.creation.cylinder(radius=radius, height=length,
                                     sections=128)
    if direction == "x":
        model.apply_transform(trimesh.transformations.rotation_matrix(
            np.pi / 2, [0, 1, 0]))
    if direction == "y":
        model.apply_transform(trimesh.transformations.rotation_matrix(
            np.pi / 2, [1, 0, 0]))
    model.apply_translation(center)
    return model


def restore_box():
    # Rounded measurements from sections through Box.ply, in mm.
    # We use smooth walls and bores because the scan does not resolve threads.
    outer = [cylinder(30.65, 27, [0, 0, 13.5]),
             cylinder(12.5, 98, [0, 0, 12.5], "x"),
             cylinder(12.5, 92, [0, 0, 12.5], "y")]
    holes = [cylinder(28.65, 40, [0, 0, 23]),
             cylinder(10, 100, [0, 0, 12.5], "x"),
             cylinder(10, 94, [0, 0, 12.5], "y")]
    body = trimesh.boolean.union(outer, engine="manifold")
    body = trimesh.boolean.difference([body] + holes, engine="manifold")
    # Two approximate mounting bosses on the inside wall.
    bosses = [cylinder(4, 24, [-10, y, 15]) for y in (-23, 23)]
    body = trimesh.boolean.union([body] + bosses, engine="manifold")
    screws = [cylinder(1.6, 12, [-10, y, 25]) for y in (-23, 23)]
    body = trimesh.boolean.difference([body] + screws, engine="manifold")
    return body


def main():
    parser = argparse.ArgumentParser(description="Rebuild the three qualifier models.")
    parser.add_argument("--scans", type=Path,
                        help="Optional folder with the three original PLY scans.")
    args = parser.parse_args()

    OUTPUT.mkdir(exist_ok=True)
    info = {name: {} for name in ("Boat", "Box", "Tower")}
    # Use our recorded dimensions when the organizer's scans are unavailable.
    tower_size = (12.5, 24.2)
    if args.scans:
        for name in info:
            vertices, faces = read_ply(args.scans / (name + ".ply"))
            info[name].update(input_vertices=len(vertices), input_faces=len(faces))
            if name == "Tower":
                tower_size = fit_tower(vertices)

    boat = read_benchy()
    tower, dimensions = restore_tower(*tower_size)
    box = restore_box()
    info["Tower"]["fitted_dimensions"] = dimensions
    info["Box"]["design_dimensions_mm"] = {
        "overall_x": 98, "overall_y": 92, "height": 27,
        "body_outer_diameter": 61.3, "body_inner_diameter": 57.3,
        "port_outer_diameter": 25, "port_inner_diameter": 20,
        "floor_thickness": 3, "boss_diameter": 8,
        "screw_hole_diameter": 3.2}

    for name, model in [("Boat", boat), ("Box", box), ("Tower", tower)]:
        path = OUTPUT / ("No_Fix_Submission_" + name + ".stl")
        model.export(path)
        # Check the saved file, not only the model still in memory.
        saved = trimesh.load_mesh(path)
        if not saved.is_watertight or not saved.is_winding_consistent or saved.volume <= 0:
            raise ValueError("Mesh check failed for " + name)
        info[name]["output_faces"] = len(saved.faces)
        info[name]["watertight"] = bool(saved.is_watertight)
        info[name]["extents_mm"] = saved.extents.tolist()
        info[name]["volume_mm3"] = float(saved.volume)
        print(name, "saved:", len(saved.faces), "faces, closed mesh")

    (ROOT / "results.json").write_text(json.dumps(info, indent=2) + "\n")


if __name__ == "__main__":
    main()
