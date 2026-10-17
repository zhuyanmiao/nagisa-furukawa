#!/usr/bin/env python3
"""Build V3/V4 atlases from separately generated, registered character artwork.

Requires Python 3.9+ and Pillow 10.1+. No API calls and no inherited V2 pixels.
Head boxes and anchors are deliberate art annotations, never inferred from each
frame's silhouette. Run --print-template to obtain a registration JSON template.
"""
import argparse
import json
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
CELL_W, CELL_H, PADDING = 192, 208, 8
COUNTS = [6, 8, 8, 4, 5, 8, 6, 6, 6]
NAMES = ["idle", "running-right", "running-left", "waving", "jumping",
         "failed", "waiting", "running", "review"]
SOURCE_LAYOUTS = {
    "idle": (1, 1), "run-right": (2, 2), "run-left": (2, 2),
    "wave": (2, 2), "jump": (1, 1), "failed": (1, 1),
    "waiting": (1, 1), "review": (1, 1),
}
SOURCE_FOR_STATE = ["idle", "run-right", "run-left", "wave", "jump",
                    "failed", "waiting", "run-right", "review"]
FRAME_MAPS = [
    [0] * 6, [0, 0, 1, 1, 2, 2, 3, 3], [0, 0, 1, 1, 2, 2, 3, 3],
    [0, 1, 2, 3], [0] * 5, [0] * 8, [0] * 6,
    [0, 0, 1, 2, 2, 3], [0] * 6,
]


def template():
    """Coordinates are normalized within an extracted source tile by default."""
    return {
        "target_head_width": 82,
        "alpha_cutoff": 4,
        "output_dir": "nagisa-furukawa-v3",
        "report_dir": "artwork/v3",
        "derived_source_dir": "artwork/v3/sources",
        "sources": {
            name: {
                "path": "artwork/v3/sources/" + name + ".png",
                "layout": list(layout),
                "frames": [{"head_box": None, "anchor": None}
                           for _ in range(layout[0] * layout[1])],
            } for name, layout in SOURCE_LAYOUTS.items()
        },
        "states": {
            name: {"root": [96, 198], "frame_map": FRAME_MAPS[row]}
            for row, name in enumerate(NAMES)
        },
    }


def number_list(value, length, label):
    if (not isinstance(value, list) or len(value) != length
            or any(not isinstance(v, (int, float)) or not math.isfinite(v)
                   for v in value)):
        raise ValueError(label + " must be a list of " + str(length) + " finite numbers")
    return value


def rectangle(spec, normalized_key, pixel_key, size, label, required=True):
    value = spec.get(pixel_key)
    if value is not None:
        box = number_list(value, 4, label)
    else:
        value = spec.get(normalized_key)
        if value is None:
            if not required:
                return None
            raise ValueError(label + " is missing; register this frame before building")
        box = number_list(value, 4, label)
        if not all(0 <= v <= 1 for v in box):
            raise ValueError(label + " normalized coordinates must lie in [0, 1]")
        box = [box[0] * size[0], box[1] * size[1],
               box[2] * size[0], box[3] * size[1]]
    left, top, right, bottom = box
    if not (0 <= left < right <= size[0] and 0 <= top < bottom <= size[1]):
        raise ValueError(label + " is outside the image or has nonpositive area")
    return tuple(box)


def anchor_point(spec, size, label):
    value = spec.get("anchor_pixels")
    if value is not None:
        return tuple(number_list(value, 2, label))
    value = spec.get("anchor")
    if value is None:
        raise ValueError(label + " is missing; register this frame before building")
    x, y = number_list(value, 2, label)
    if not (0 <= x <= 1 and 0 <= y <= 1):
        raise ValueError(label + " normalized coordinates must lie in [0, 1]")
    return x * size[0], y * size[1]


def clean_alpha(image, cutoff):
    """Clear only near-transparent residue; do not delete detached props."""
    result = image.copy().convert("RGBA")
    result.putalpha(result.getchannel("A").point(lambda a: 0 if a <= cutoff else a))
    pixels = result.load()
    for y in range(result.height):
        for x in range(result.width):
            if pixels[x, y][3] == 0:
                pixels[x, y] = (0, 0, 0, 0)
    return result


def prepare_wave_composite(config, root, cutoff):
    """Copy only the edited hand/cuff region onto the exact original body.

    The reviewed polygon excludes all eyes, nose, mouth and the main torso.
    Two genuine poses are held rather than alpha-blending ghost fingers.
    """
    spec = config.get("wave_composite")
    if not spec:
        return None
    base_path, out_path = root / spec["base"], root / spec["out"]
    base = clean_alpha(Image.open(base_path), cutoff)
    edited = clean_alpha(Image.open(out_path), cutoff).resize(base.size, Image.Resampling.LANCZOS)
    dx, dy = spec.get("edited_offset", [0, 0])
    if dx or dy:
        shifted = Image.new("RGBA", base.size)
        shifted.alpha_composite(edited, (int(dx), int(dy)))
        edited = shifted
    mask = Image.new("L", base.size)
    polygon = spec["hand_cuff_polygon_pixels"]
    ImageDraw.Draw(mask).polygon([tuple(point) for point in polygon], fill=255)
    locked = Image.composite(edited, base, mask)
    # Verify locking rather than merely describing the operation as local.
    before, after, allowed = base.load(), locked.load(), mask.load()
    changed, outside = 0, 0
    for y in range(base.height):
        for x in range(base.width):
            if before[x, y] != after[x, y]:
                changed += 1
                outside += allowed[x, y] == 0
    if outside:
        raise ValueError("Wave compositing changed pixels outside the reviewed hand/cuff polygon")
    derived = root / config.get("derived_source_dir", "artwork/v3/sources")
    derived.mkdir(parents=True, exist_ok=True)
    paths = [derived / "wave-locked-in.png", derived / "wave-locked-out.png"]
    base.save(paths[0])
    locked.save(paths[1])
    return {"base": str(base_path.relative_to(root)), "edit": str(out_path.relative_to(root)),
            "base_size": list(base.size), "polygon_pixels": polygon,
            "edited_offset": [dx, dy], "changed_pixels": changed,
            "changed_pixels_outside_polygon": outside,
            "method": "Two generated hand poses; original body and face outside polygon remain pixel-identical."}


def prepare_run_composites(config, root, cutoff):
    """Lock the approved head/torso while registering generated lower poses."""
    records = []
    for name, spec in config.get("run_composites", {}).items():
        base_sheet = clean_alpha(Image.open(root / spec["base"]), cutoff)
        base = base_sheet.crop(tuple(spec["base_crop_pixels"]))
        base_head_width = spec["base_head_width"]
        for index, frame in enumerate(spec["frames"]):
            if index == 0:
                locked = base.copy()
            else:
                donor = clean_alpha(Image.open(root / frame["path"]), cutoff)
                if frame.get("crop_pixels"):
                    donor = donor.crop(tuple(frame["crop_pixels"]))
                scale = base_head_width / frame["head_width"]
                donor = donor.resize((round(donor.width * scale), round(donor.height * scale)),
                                     Image.Resampling.LANCZOS)
                dx = round(spec["pelvis_pixels"][0] - frame["pelvis_pixels"][0] * scale)
                dy = round(spec["pelvis_pixels"][1] - frame["pelvis_pixels"][1] * scale)
                aligned = Image.new("RGBA", base.size)
                aligned.alpha_composite(donor, (dx, dy))
                mask = Image.new("L", base.size)
                top, bottom = spec["blend_rows"]
                draw = ImageDraw.Draw(mask)
                draw.rectangle((0, 0, base.width, top), fill=255)
                for y in range(top + 1, bottom):
                    draw.line((0, y, base.width, y), fill=round(255 * (bottom - y) / (bottom - top)))
                # Preserve the schoolbag as well as head/torso; otherwise its
                # lower edge could jump across the waist-level blend boundary.
                draw.rectangle(tuple(spec["bag_lock_rectangle"]), fill=255)
                locked = Image.composite(base, aligned, mask)
                if locked.crop((0, 0, base.width, top)).tobytes() != base.crop((0, 0, base.width, top)).tobytes():
                    raise ValueError(name + " changed locked upper-body pixels")
            derived = root / config.get("derived_source_dir", "artwork/v3/sources")
            derived.mkdir(parents=True, exist_ok=True)
            output = derived / (name + "-locked-" + str(index) + ".png")
            locked.save(output)
            records.append({"source": name, "frame": index, "output": str(output.relative_to(root)),
                            "upper_body_rows_locked": spec["blend_rows"][0],
                            "method": "Original head, torso and bag; generated lower pose aligned at manually registered pelvis."})
    return records


def read_sources(config, root, cutoff):
    sources = {}
    annotations = []
    for name, default_layout in SOURCE_LAYOUTS.items():
        spec = config.get("sources", {}).get(name)
        if spec is None:
            raise ValueError("Missing source registration: " + name)
        path = root / spec.get("path", "artwork/v3/sources/" + name + ".png")
        if not path.is_file():
            raise FileNotFoundError("Source artwork does not exist: " + str(path))
        with Image.open(path) as opened:
            if "A" not in opened.getbands():
                raise ValueError(name + " has no alpha channel; supply transparent source artwork")
            source = clean_alpha(opened, cutoff)
        if source.getchannel("A").getextrema()[0] != 0:
            raise ValueError(name + " has no fully transparent background pixels")
        cols, rows = spec.get("layout", default_layout)
        if (not isinstance(cols, int) or not isinstance(rows, int)
                or cols <= 0 or rows <= 0):
            raise ValueError(name + " layout must contain two positive integers")
        frames = spec.get("frames", [])
        expected = default_layout[0] * default_layout[1]
        if len(frames) != expected:
            raise ValueError(name + " requires " + str(expected) + " registered source frames")
        target_head = float(spec.get("target_head_width", config.get("target_head_width", 82)))
        if not 20 <= target_head <= 120:
            raise ValueError(name + " target head width must be between 20 and 120 pixels")
        prepared = []
        for index, frame_spec in enumerate(frames):
            label = name + "/" + str(index)
            frame_source = source
            frame_path = path
            if frame_spec.get("path"):
                frame_path = root / frame_spec["path"]
                with Image.open(frame_path) as opened:
                    if "A" not in opened.getbands():
                        raise ValueError(label + " alternate image has no alpha channel")
                    frame_source = clean_alpha(opened, cutoff)
            crop = rectangle(frame_spec, "crop", "crop_pixels", frame_source.size,
                             label + " crop", required=False)
            if crop is None:
                if frame_path != path:
                    crop = (0, 0, frame_source.width, frame_source.height)
                else:
                    col, row = index % cols, index // cols
                    if row >= rows:
                        raise ValueError(label + " is outside its declared sheet layout")
                    crop = (round(col * source.width / cols), round(row * source.height / rows),
                            round((col + 1) * source.width / cols), round((row + 1) * source.height / rows))
            crop = tuple(round(v) for v in crop)
            tile = frame_source.crop(crop)
            head = rectangle(frame_spec, "head_box", "head_box_pixels", tile.size,
                             label + " head box")
            anchor = anchor_point(frame_spec, tile.size, label + " root anchor")
            painted = tile.getchannel("A").getbbox()
            if painted is None:
                raise ValueError(label + " is empty after transparent residue cleanup")
            # Tight extraction saves memory only. Registration retains the
            # original coordinate system by subtracting the crop origin.
            sprite = tile.crop(painted)
            anchor_in_sprite = (anchor[0] - painted[0], anchor[1] - painted[1])
            head_width = head[2] - head[0]
            annotation = {
                "source": name, "source_frame": index, "path": str(frame_path.relative_to(root)),
                "source_size": list(frame_source.size), "source_crop": list(crop),
                "head_box_in_tile": list(head), "anchor_in_tile": list(anchor),
                "painted_box_in_tile": list(painted),
                "target_head_width": target_head, "base_scale": target_head / head_width,
                "source_touches_crop_edge": bool(
                    min(painted[0], painted[1], tile.width - painted[2], tile.height - painted[3]) == 0),
            }
            prepared.append({"image": sprite, "anchor": anchor_in_sprite,
                             "scale": target_head / head_width, "annotation": annotation})
            annotations.append(annotation)
        sources[name] = prepared
    return sources, annotations


def motion(row, frame, count):
    """Small whole-silhouette motion only; no synthesized eyes or anatomy."""
    if row == 4:
        # The character and its prop remain one registered source group. Give
        # that group a real flight arc and restrained compression; individual
        # limbs are not rearticulated. Version-specific config can shorten it.
        return {"dx": 0, "dy": [0, -10, -22, -10, 0][frame],
                "scale_x": 1, "scale_y": [0.96, 1.01, 1, 1.01, 0.96][frame]}
    if row in (0, 5, 6, 8):
        # Last and first frames agree. Changes remain below roughly one pixel
        # at the intended display size; facial expression stays unchanged.
        phase = math.sin(math.pi * frame / (count - 1)) ** 2
        return {"dx": 0, "dy": 0, "scale_x": 1, "scale_y": 1 + 0.004 * phase}
    return {"dx": 0, "dy": 0, "scale_x": 1, "scale_y": 1}


def render_frame(prepared, root, transform, cutoff, label):
    sprite = prepared["image"]
    scale_x = prepared["scale"] * transform["scale_x"]
    scale_y = prepared["scale"] * transform["scale_y"]
    width = max(1, round(sprite.width * scale_x))
    height = max(1, round(sprite.height * scale_y))
    if width > CELL_W * 3 or height > CELL_H * 3:
        raise ValueError(label + " is implausibly large; check the head box")
    resized = clean_alpha(sprite.resize((width, height), Image.Resampling.LANCZOS), cutoff)
    # Use actual raster dimensions so rounding never changes the anchor rule.
    actual_x, actual_y = width / sprite.width, height / sprite.height
    left = round(root[0] + transform["dx"] - prepared["anchor"][0] * actual_x)
    top = round(root[1] + transform["dy"] - prepared["anchor"][1] * actual_y)
    painted = resized.getchannel("A").getbbox()
    if painted is None:
        raise ValueError(label + " became empty during scaling")
    bounds = (left + painted[0], top + painted[1], left + painted[2], top + painted[3])
    margins = [bounds[0], bounds[1], CELL_W - bounds[2], CELL_H - bounds[3]]
    if min(margins) < PADDING:
        raise ValueError(label + " violates 8px safety margin: bounds=" + str(bounds)
                         + "; adjust registration, global head scale or pose, not per-frame clipping")
    canvas = Image.new("RGBA", (CELL_W, CELL_H))
    canvas.alpha_composite(resized, (left, top))
    return canvas, {"destination_bounds": list(bounds), "margins": margins,
                    "root": list(root), "transform": transform,
                    "raster_scale": [actual_x, actual_y]}


def make_contact(atlas, path, version_label="V3"):
    label_height = 26
    contact = Image.new("RGB", (CELL_W * 8, (CELL_H + label_height) * 9), "#f9f5ef")
    draw = ImageDraw.Draw(contact)
    font = ImageFont.load_default(size=16)
    for row, name in enumerate(NAMES):
        y0 = row * (CELL_H + label_height)
        draw.rectangle((0, y0, CELL_W * 8, y0 + label_height), fill="#3d3834")
        draw.text((8, y0 + 4), name + " / " + str(COUNTS[row]) + " frames / " + version_label, font=font, fill="white")
        for col in range(8):
            x0 = col * CELL_W
            for y in range(0, CELL_H, 16):
                for x in range(0, CELL_W, 16):
                    color = "#e7e0d8" if (x // 16 + y // 16) % 2 else "#fffdf9"
                    draw.rectangle((x0 + x, y0 + label_height + y,
                                    x0 + x + 15, y0 + label_height + y + 15), fill=color)
            tile = atlas.crop((col * CELL_W, row * CELL_H, (col + 1) * CELL_W, (row + 1) * CELL_H))
            contact.paste(tile, (x0, y0 + label_height), tile)
            draw.rectangle((x0, y0 + label_height, x0 + CELL_W - 1,
                            y0 + label_height + CELL_H - 1), outline="#b9b0a7")
    contact.save(path)


def build(config, root):
    cutoff = config.get("alpha_cutoff", 4)
    if not isinstance(cutoff, int) or not 0 <= cutoff <= 16:
        raise ValueError("alpha_cutoff must be an integer between 0 and 16")
    wave_record = prepare_wave_composite(config, root, cutoff)
    run_records = prepare_run_composites(config, root, cutoff)
    sources, annotations = read_sources(config, root, cutoff)
    atlas = Image.new("RGBA", (CELL_W * 8, CELL_H * 9))
    records = []
    # All sources and registrations are checked before output creation. A bad
    # annotation never silently falls back to tight-box centering.
    for row, name in enumerate(NAMES):
        spec = config.get("states", {}).get(name, {})
        root_point = number_list(spec.get("root", [96, 198]), 2, name + " root")
        mapping = spec.get("frame_map", FRAME_MAPS[row])
        if len(mapping) != COUNTS[row] or any(not isinstance(i, int) for i in mapping):
            raise ValueError(name + " frame_map must contain exactly " + str(COUNTS[row]) + " integers")
        overrides = spec.get("transforms", [{} for _ in mapping])
        if len(overrides) != len(mapping):
            raise ValueError(name + " transforms count must match its frame count")
        mirror_x = spec.get("mirror_x", False)
        if not isinstance(mirror_x, bool):
            raise ValueError(name + " mirror_x must be a boolean")
        for frame, source_index in enumerate(mapping):
            source_name = SOURCE_FOR_STATE[row]
            if not 0 <= source_index < len(sources[source_name]):
                raise ValueError(name + " references a nonexistent source frame")
            transform = motion(row, frame, COUNTS[row])
            for key, value in overrides[frame].items():
                if key not in transform or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise ValueError(name + " invalid transform override: " + str(key))
                transform[key] = value
            if transform["scale_x"] <= 0 or transform["scale_y"] <= 0:
                raise ValueError(name + " scale must be positive")
            prepared = sources[source_name][source_index]
            cell, details = render_frame(prepared, root_point, transform, cutoff, name + "/" + str(frame))
            if mirror_x:
                # Reuse one registered drawing for both movement directions.
                # Mirror the final cell to avoid a second resampling or a
                # one-pixel registration difference between directions.
                cell = cell.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
                left, top, right, bottom = details["destination_bounds"]
                details["destination_bounds"] = [CELL_W - right, top, CELL_W - left, bottom]
                margins = details["margins"]
                details["margins"] = [margins[2], margins[1], margins[0], margins[3]]
                details["root"] = [CELL_W - root_point[0], root_point[1]]
            details["mirrored_x"] = mirror_x
            atlas.alpha_composite(cell, (frame * CELL_W, row * CELL_H))
            records.append({"row": row, "state": name, "frame": frame,
                            "source": source_name, "source_frame": source_index, **details})
    output = root / config.get("output_dir", "nagisa-furukawa-v3")
    research = root / config.get("report_dir", "artwork/v3")
    output.mkdir(parents=True, exist_ok=True)
    research.mkdir(parents=True, exist_ok=True)
    atlas_path = output / "spritesheet.webp"
    atlas.save(atlas_path, format="WEBP", lossless=True, exact=True)
    with Image.open(atlas_path) as opened:
        encoded = opened.convert("RGBA")
    failures, blank_cells = [], []
    for row, count in enumerate(COUNTS):
        for col in range(8):
            tile = encoded.getchannel("A").crop((col * CELL_W, row * CELL_H,
                                                 (col + 1) * CELL_W, (row + 1) * CELL_H))
            bounds = tile.getbbox()
            if col >= count:
                blank_cells.append({"row": row, "column": col, "clear": bounds is None})
                if bounds is not None:
                    failures.append("Content in reserved blank cell " + str((row, col)))
            elif bounds is None or min(bounds[0], bounds[1], CELL_W - bounds[2], CELL_H - bounds[3]) < PADDING:
                failures.append("Active cell is empty or clipped " + str((row, col)))
    if encoded.size != (1536, 1872) or len(records) != 57:
        failures.append("Encoded dimensions or frame count are incorrect")
    art_review = config.get("art_review", {})
    status = ("technical_pass_with_art_blockers" if art_review.get("hard_blockers")
              else "technical_pass_requires_art_and_runtime_review")
    report = {
        "status": status if not failures else "failed",
        "output_dir": str(output.relative_to(root)), "report_dir": str(research.relative_to(root)),
        "encoded_size": list(encoded.size), "encoded_mode": encoded.mode,
        "active_counts": COUNTS, "active_frames": len(records),
        "blank_cells": blank_cells, "minimum_padding": PADDING,
        "all_blank_cells_clear": all(item["clear"] for item in blank_cells),
        "alpha_cutoff": cutoff, "failures": failures,
        "source_registration": annotations, "frames": records,
        "wave_local_composite": wave_record,
        "run_lower_body_composites": run_records,
        "art_review": art_review,
        "limitations": [
            "Four genuine generated gait poses are held across eight atlas frames; no new leg anatomy is synthesized.",
            "Single-pose states use restrained whole-silhouette breathing, not reconstructed facial or skeletal animation.",
            "Jump translates and gently compresses one character-and-prop source group; no independent limb or mount articulation is synthesized.",
            "Technical checks do not establish character fidelity, expression quality, loop quality or Codex runtime behavior.",
        ],
    }
    (research / "validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if failures:
        raise ValueError("Encoded atlas failed validation: " + "; ".join(failures))
    make_contact(encoded, output / "contact-sheet.png", config.get("version_label", "V3"))
    print(json.dumps({"status": report["status"], "atlas": str(atlas_path),
                      "active_frames": 57, "blank_cells": 15,
                      "size": list(encoded.size), "minimum_padding": PADDING}, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Manually reviewed source registration JSON")
    parser.add_argument("--root", type=Path, default=ROOT, help="Repository root, defaults to this script's parent repository")
    parser.add_argument("--print-template", action="store_true", help="Print an unregistered JSON template without reading source artwork")
    args = parser.parse_args()
    if args.print_template:
        print(json.dumps(template(), ensure_ascii=False, indent=2))
        return
    if not args.config:
        parser.error("--config is required; use --print-template and register head boxes and anchors")
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        build(config, args.root.resolve())
    except (ValueError, FileNotFoundError, KeyError, TypeError) as error:
        parser.exit(1, "V3 build failed: " + str(error) + "\n")


if __name__ == "__main__":
    main()
